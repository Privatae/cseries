// Domain model for the Circles app. Deliberately independent of the `ai`
// package's UIMessage type — the AI Elements components take primitive props,
// so we keep our own small, explicit shapes.

export type ChatStatus = "ready" | "submitted" | "streaming" | "error";

export type ToolState = "input-available" | "output-available" | "output-error";

export interface ToolInvocation {
  id: string; // toolCallId
  name: string; // toolName
  args: unknown; // input
  summary: string; // one-line summary of args for the collapsed header
  /** The op/action the tool ran, as words ("grep", "set desktop") — the
   * header reads "<Tool> <verb>"; the summary is the quoted subject. */
  verb?: string;
  result?: unknown; // output (envelope from the `a:` frame)
  state: ToolState;
  /** "agent" when this invocation dispatched a background agent (the
   * `dispatch` primitive); "tool" (default) for the host's own tool calls.
   * Agents render distinctly — work the host DELEGATED, not ran itself. */
  kind?: "tool" | "agent";
  /** The dispatched agent's task id (kind === "agent"). */
  taskId?: string;
  /** Persisted chips only: the inner tool of an `operate` call
   * (session.py tool_call_chip `operate_tool`). */
  operateTool?: string;
  /** Persisted chips only: the bounded result record the chip kept
   * (runtime/tool_trail.py digest — ids, preview_url, status). */
  digest?: Record<string, unknown>;
}

/** Attribution for an assistant turn that is a background agent's delivered
 * result (deliver_agent_result stamps origin='agent' on the turn row). */
export interface AgentAttribution {
  taskId?: string;
  title?: string;
  status?: string; // completed | failed | timed_out
  costUsd?: number | null;
}

/** Per-turn run metrics — stamped into the turn row's metadata by the
 * runtime at write (session.py), or measured client-side for the turn
 * that just streamed. All fields optional: old rows predate the stamp. */
export interface TurnMetrics {
  gen_ms?: number | null;
  tokens_total?: number | null;
  /** True total context (cached + uncached, lane-normalized). Rows written
   * before 2026-07-24 carry the wire's uncached remainder here instead. */
  tokens_in?: number | null;
  tokens_out?: number | null;
  /** Prefix tokens served FROM cache this turn (cache reads). */
  tokens_cached?: number | null;
  /** Prefix tokens WRITTEN to cache this turn (cache creation). */
  tokens_cache_write?: number | null;
  model?: string | null;
  /** Provenance — WHO ANSWERED, printed under the turn (2026-08-18). */
  provider?: string | null;
  /** List-price estimate for the turn at the served model's rates
   * (2026-09-18). null = not per-token priced; see price_basis. */
  est_cost_usd?: number | null;
  /** Why the dollars are what they are: "list" (priced) | "free" (local) |
   * "subscription" (flat plan) | "unpriced" (no published rate) | "partial"
   * (priced, but some calls reported no usage, so it covers only the rest). The word is
   * printed where the price would go, so "$0" never means "unknown". */
  price_basis?: string | null;
  failed_over?: {
    from_provider?: string | null;
    from_model?: string | null;
    to_provider?: string | null;
    to_model?: string | null;
    primary_error?: string | null;
  } | null;
  /** The Host's close_turn signal for this turn (the Review button). */
  close_turn?: import("@kit/close-turn").CloseTurnSignal | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  tools: ToolInvocation[];
  createdAt: string;
  /** WHO wrote it, for threads with someone other than your own host (Peers,
   * People): the person's or host's name. Absent on the self thread. */
  author?: string;
  /** The host that carried a person's message ("Alex via host-a"). */
  via?: string;
  /** "person" when a human wrote it, "entity" when a host did. */
  authorKind?: "self" | "person" | "entity";
  /** A host's message that named a person your Host could not verify: the
   * address it named ("Juno · host · says this is from ana@example.com"). */
  claimedPerson?: string;
  /** The user's OWN Host wrote this turn. Set only where the message is built
   * knowing its thread: a self-thread row (mapThreadMessages ownThread) and
   * the live stream, which only the self thread has. Absent = not proven, and
   * nothing runs. author/authorKind cannot decide it: the server sends a
   * self-thread reply as author_kind "entity" + the Host's name
   * (routes/circles.py _normalize_turn_rows), the same shape as a peer host's
   * message in a Peers thread. */
  ownHost?: boolean;
  /** Duration + token use for this turn (assistant turns). */
  metrics?: TurnMetrics;
  /** Terminal error surfaced on the assistant turn (`3:` frame or HTTP). */
  error?: string;
  /** True while the assistant turn is still streaming. */
  streaming?: boolean;
  /** A reattached turn (a reload mid-turn, a wake) whose replay of what it
   * did before has not yet been followed by a live frame: its words are not
   * arriving now, so nothing may say the Host is writing them. */
  replaying?: boolean;
  /** Your Host's one conversation: a reply streaming on another topic beside
   * the turn this window follows (useCirclesChat sendBeside). It draws its
   * own working line, and the registry id of its turn rides `besideTurnId`
   * once its turn frame arrives. */
  beside?: boolean;
  besideTurnId?: string;
  /** Live reasoning feed (`g:` frames) — what the host is thinking RIGHT NOW.
   * Streams into the bubble while the reply is being worked out; the era of
   * three silent dots is over. */
  reasoning?: string;
  /** Set when this turn is a dispatched agent's delivered result — renders
   * the agent marker so it's distinguishable from the host just talking. */
  agent?: AgentAttribution;
  /** The turn that wrote it: from the `2:[{type:"session"}]` frame on a live
   * echo, from `turn_token` on a saved row. mergeHistory swaps an echo for
   * the saved row carrying the same token. */
  turnToken?: string;
  /** A message typed while the turn ran (Circles nudge). Never saved as a
   * row, so the merge keeps it beside its turn instead of swapping it. */
  nudge?: boolean;
  /** A nudge echo drawn when it was sent (the cards, useCirclesChat
   * echoNudges): the id it was nudged with, and whether the turn has read it
   * (its nudge_read receipt). */
  nudgeId?: string;
  nudgeRead?: boolean;
  /** A message of a person's chat given to this turn (Ask Ivy, cards slice
   * 4): what the turn's user row records, {kind, ref, message_id}, and, on a
   * live echo, the quote as the person saw it. */
  carry?: CarryRef | CarryRef[];
  /** A person's chat: a reading notice ("on" | "off", server/circles_reading
   * .py NOTICE_KEY), drawn from its value, never from the row's words. */
  readingNotice?: "on" | "off";
  /** Your Host's one conversation (GET /api/circles/feed): the topic this
   * row was said in, and whether the row is the Host's topic-switch line. */
  topic?: FeedTopic;
  topicMarker?: boolean;
}

/** A topic of the one conversation: a thread id or "original". */
export interface FeedTopic {
  id: string;
  title: string;
  root?: boolean;
}

/** One carried message (POST /api/circles/messages `carry`). */
export interface CarryRef {
  kind: "dm";
  ref: string;
  message_id: string;
  /** The live echo's own copy of the quote and whose it is; never sent. */
  quote?: string;
  who?: string;
}

// ---- Model picker (server/routes/inference.py:get_model_catalog) ----

/** A {provider, model_id} pick, the shape the backend's _normalize_thread_override
 * accepts and that /messages, /replay, /ab all read. */
export interface ModelPick {
  provider: string; // "privatae" | "local" | ...
  model_id: string;
}

/** One row in the in-chat model picker, flattened from the inference catalog. */
export interface CatalogModel {
  id: string;
  name: string;
  provider: string; // "privatae" | "local"
  /** USD per 1M input tokens (null = unknown/local-free). */
  inputPerM?: number | null;
  /** USD per 1M output tokens (null = unknown/local-free). */
  outputPerM?: number | null;
  /** Max context window in tokens, if known. */
  contextLength?: number | null;
}

// ---- Attachments (server/routes/circles.py /messages, Workbench blocks) ----

/** Workbench-format content block the /messages endpoint accepts:
 * [{name?, content_block:{type:image|document, source:{type:base64, media_type, data}}}] */
export interface AttachmentBlock {
  name?: string;
  content_block: {
    type: "image" | "document";
    source: { type: "base64"; media_type: string; data: string };
  };
}

// ---- Detached turns (server/routes/circles.py /turns/*, 2026-07-23) ----

/** A registered turn in the server's turn registry — running or recently
 * finished. Returned by GET /api/circles/turns/active and carried on the
 * stream's 2:[{type:"turn"}] metadata frame. */
export interface ActiveTurn {
  turn_id: string;
  status: "running" | "done" | "error" | "cancelled" | "timeout";
  started_at: number;
  elapsed_s: number;
  user_message: string;
  session_id?: string | null;
  seq: number;
  /** The conversation the turn is in (server/turn_registry.py
   * compose_thread_key): which page of the cards' Host is working. */
  thread_key?: string | null;
}


export interface Target {
  kind: string; // self | dm | entity | circle
  id: string;
  label: string;
}

// ---- Circles threads (server/routes/circles.py /threads, 2026-09-21) ----
// A thread is a scope within the self conversation. "original" is the
// sentinel id for the pre-existing conversation — not a row, keeps today's
// behaviour exactly.

export interface Thread {
  id: string; // "original" | uuid
  title: string | null;
  topic: string | null;
  created_at: string | null;
  closed_at: string | null;
  origin: "legacy" | "user" | "split";
  parent_thread_id: string | null;
  turn_count: number;
  /** Turn types (2026-09-26): the type this thread is bound to, when one
   * is — the type's own thread. Absent/null on every thread before types. */
  type_id?: string | null;
  type_name?: string | null;
}

/** One of the entity's ACTIVE turn types (server/routes/turn_types.py) —
 * the dropdown's row. `thread_id` is the type's thread once it has one. */
export interface TurnType {
  id: string;
  name: string;
  thread_id: string | null;
  skill: string | null;
  tool_groups: string[];
  keyword_count: number;
}

/** A soft topic-flip marker rendered in-line in the chat — never a divider
 * that reads as a cut (design §8: users must not think context was cut). */
export interface Marker {
  summary_id: number;
  topic: string;
  prev_topic: string | null;
  at: string; // iso
  from_turn_id: number | null;
}

// ---- Reviews / tickets (server/routes/tickets.py) ----

// A 'hands' option may ask for a credential (2026-09-24): the card seals the
// pasted value into Keys under `name` and decides the ticket with that name
// in the note. The value never rides the ticket, and the Host only ever gets
// the name.
export interface CredentialRequest {
  name: string;
  cred_type: "api_key" | "opaque";
  purpose?: string;
}

export interface TicketOption {
  id: string;
  label?: string;
  consequence?: string;
  recommend?: boolean;
  pattern_template?: string;
  credential_request?: CredentialRequest;
}

// What a linked Workbench phase last reported onto an open ticket
// (server/routes/tickets.py on_phase_outcome). A 'complete' landing decides
// the ticket instead, so this only ever carries failed / review.
export interface PhaseLast {
  status: string;
  note?: string;
  at?: string;
}

export interface Ticket {
  id: number;
  ticket_uuid: string;
  source: string;
  source_ref?: string | null;
  circle_id?: number | null;
  requester?: {
    kind?: string;
    handle?: string;
    display_name?: string;
  } | null;
  sitrep: string;
  options: TicketOption[];
  context?: unknown;
  status: string; // open | decided | auto_resolved | expired | dismissed
  priority: number;
  decision?: {
    option_id: string;
    option_label?: string;
    and_similar?: boolean;
    note?: string;
  } | null;
  decided_at?: string | null;
  pattern_id?: number | null;
  opened_at: string;
  expires_at?: string | null;
  decided_by?: string | null; // only present on GET /api/tickets/{id}
  metadata?: unknown;
}

export interface BadgeCounts {
  by_circle: Record<string, number>;
  total: number;
}

// ---- Support tickets (server/routes/support.py) ----

export type SupportCategory = "bug" | "question" | "feature" | "billing" | "access";

export interface SharedItem {
  key: string;
  label: string;
  kind: string; // versions | project | errors | inference | file
  content: string;
  included: boolean;
}

export interface SupportMessage {
  author: "host" | "user" | "platform";
  body: string;
  at: string;
}

export interface SupportTicket {
  id: number;
  ticket_uuid: string;
  category: SupportCategory;
  project_id?: number | null;
  project_name: string;
  title: string;
  severity: "low" | "normal" | "high";
  problem: string;
  context?: string;
  recommendation?: string;
  pushback?: string;
  repro_steps?: string;
  expected?: string;
  actual?: string;
  shared_context?: SharedItem[];
  shared_items?: number;
  /** The draft's version (GET /api/support/{id}); Send hands it back. */
  revision?: number;
  status: "draft" | "sent" | "discarded" | "closed";
  platform_id?: number | null;
  platform_status: string; // received | reproducing | pr_open | needs_info | approved | fixed | rejected
  platform_ref: string;
  resolution: string;
  pr_url: string;
  thread?: SupportMessage[];
  thread_len?: number;
  opened_at: string;
  sent_at?: string | null;
  updated_at: string;
  closed_at?: string | null;
}

/** Where this Host's support requests can go (GET /api/support/route,
 * server/support_reach.py): a private ticket (platform, or a network that
 * offers one), the members' board, or nowhere at Privatae (standalone). */
export interface SupportRoute {
  lane: "platform" | "network" | "board" | "standalone" | "member_none";
  can_send: boolean;
  /** The owner's sentence when a ticket can't be sent; empty when it can. */
  say: string;
  /** Why a private ticket can't go, one sentence (server/support_reach.py
   * head_of); empty when it can. */
  head: string;
  /** Where a request can go instead, in the server's own words
   * (support_reach.offer_of); the card shows it as it is. */
  offer: string;
  security: { email: string; policy: string; say: string };
  /** A new issue on the public repository (standalone). */
  issue_url?: string;
  board?: boolean;
}

export interface SupportBadges {
  drafts: number;
  needs_info: number;
  total: number;
}

// ---- Contacts (server/routes/contacts.py) ----

export interface Contact {
  id: number;
  handle: string;
  display_name: string | null;
  kind: "host" | "human";
  notes: string | null;
  status: "active" | "pending" | "blocked";
  last_contact_at: string | null;
  created_at: string | null;
  /** The host this contact is reached through (v123); null until an exchange
   * has carried it. A person without one cannot be messaged yet. */
  peer_entity_id?: number | null;
  /** Host contacts in the list only: the blocked people reached through this
   * Host. Blocking a person blocks their Host too, without changing its status. */
  blocked_through?: string[];
}

// ---- Domain policy (server/routes/domain_policy.py) ----

export type Posture = "open" | "contacts" | "closed";

export interface PolicyRule {
  id: number;
  peer: string;
  allow_inbox: boolean;
  allow_dm: boolean;
  allow_circle_invite: boolean;
  note: string | null;
  created_at: string | null;
}

// ---- Inbox (server/routes/inbox.py) ----

export interface InboxMessage {
  message_id: string;
  thread_id: string;
  from: string;
  to: string;
  subject: string | null;
  body_format: string;
  kind: string;
  sent_at: string | null;
  delivered_at: string | null;
  read_at: string | null;
  archived_at: string | null;
  from_entity_id: number | null;
  to_entity_id: number | null;
  /** The VERIFIED person behind the message (bridge-checked column). A
   * connection request is from a person only when this is set — never
   * decided from the payload the sender wrote. */
  from_user_email?: string | null;
  body?: string | null;
  payload?: Record<string, unknown>;
  /** Set on a decision row (inbox list): which decision it delivers. */
  decision_id?: number | null;
}

// ---- Calendar (server/routes/calendar.py) ----

export interface CalendarEvent {
  event_id: string;
  entity_id?: number;
  event_type: string;
  event_at: string;
  summary: string;
  status: string; // scheduled | running | completed | failed | cancelled
  // Why a cancelled event was cancelled. "held_for_person" is a request the
  // Host is keeping for the person's next message (shown as held, not cancelled).
  cancelled_reason?: string | null;
  held?: boolean;
  source?: string;
  cost_usd?: number | null;
  tokens_in?: number | null;
  tokens_out?: number | null;
  model_id?: string | null;
  related_message_id?: string | null;
  related_artifact_uri?: string | null;
  related_project_id?: number | null;
  payload?: Record<string, unknown>;
}


