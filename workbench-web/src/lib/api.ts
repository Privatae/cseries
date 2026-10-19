// Workbench API layer. Same request/ApiResult conventions as circles-web —
// every endpoint here is a stable kernel route the plain app or circles-web
// already exercised (brain chat, projects, review sessions, /api/agents,
// workbench gitea proxy).

import type {
  ActiveTurn,
  AgentRow,
  BenchRun,
  Bucket,
  CatalogModel,
  ModuleRow,
  ModuleVersion,
  AppState,
  CodeApproval,
  CodeApprovalDetail,
  CodeRepository,
  Phase,
  PhaseStep,
  Project,
  ProjectPage,
  ProjectRepository,
  RunDetail,
  RunSession,
  SearchHit,
} from "@/lib/types";
import type { EffortOptions } from "@/lib/effort";
import type { IndexAnswer } from "@/lib/book-index";
import { indexQueryText } from "@/lib/book-index";

// Shared client core — see app-web-kit/api.ts (ApiResult, the 401-retry
// request loop, getJSON). Endpoint functions below stay app-local.
export type { ApiResult } from "@kit/api";
import {
  extractDetail,
  getJSON,
  request,
  sleep as _sleep,
  type ApiResult,
} from "@kit/api";

let idCounter = 0;
export const localId = (prefix: string) => `${prefix}-${Date.now()}-${idCounter++}`;

// ---- The ask card ----
// Answering the overlay decides the ticket the Host filed with `escalate`,
// the same route the Circles Tickets pane uses. Never `and_similar` from
// here: a learned pattern is a deliberate act in Tickets, not a click on a
// card mid-turn.
/** The open tickets of one project's conversation: the chat
 * raises each as its card. `room` is project:<id> (app-web-kit/ask.ts
 * projectRoom). */
export function listRoomTickets(room: string): Promise<ApiResult<{ tickets: unknown[] }>> {
  return getJSON<{ tickets: unknown[] }>(
    `/api/tickets?status=open&limit=50&asked_in=${encodeURIComponent(room)}`,
  );
}

/** The one-time code of an open connector pairing (read:topology) — this
 * window only; the route refuses any app that is not the person's own. */
export function fetchPairingCode(
  grantId: number,
): Promise<ApiResult<{ code: string; seconds_left: number; platform?: string }>> {
  return getJSON(`/api/network/nac/connector/${grantId}/code`);
}

/** The connector grants (no code): the chat finds the open pairing it started. */
export function listConnectors(): Promise<ApiResult<{ connectors: unknown[] }>> {
  return getJSON<{ connectors: unknown[] }>("/api/network/nac/connector");
}

// ---- Where the Workbench reopens (server/ui_last_open.py) ----

export function getLastOpenProject(): Promise<ApiResult<{ ref: string | null }>> {
  return getJSON<{ ref: string | null }>("/api/projects/last-open");
}

export function putLastOpenProject(projectId: number): Promise<ApiResult<{ ref: string }>> {
  return request("PUT", "/api/projects/last-open", { ref: String(projectId) });
}

export function getTicket(id: number): Promise<ApiResult<{ id: number; status: string }>> {
  return getJSON<{ id: number; status: string }>(`/api/tickets/${id}`);
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
  note = "",
  stepupCode: string | null = null,
): Promise<ApiResult<DecideResult>> {
  return request("POST", `/api/tickets/${id}/decide`, {
    option_id: optionId,
    and_similar: false,
    note,
    // A pipeline change adding a check that runs outside the platform: the
    // code from the person's authenticator, checked and burned by the kernel.
    ...(stepupCode ? { stepup_code: stepupCode } : {}),
  });
}

// ---- Context upload (composer paperclip → embedded project context) ----

export interface UploadResult {
  imported: number;
  skipped?: number;
  errors?: string[];
  filename?: string;
  /** Per-file embed job (null: file-lane store or no embed model). */
  job_id: number | null;
  /** Set when text rows imported but embedding was skipped (no embed model) —
   * the reason string. The strip must surface it, not claim plain "stored". */
  embed_skipped?: string | null;
  kind?: string;
  /** The route answers some refusals as HTTP 200 + {error} (file too large,
   * empty file) — an upload that did NOT land. */
  error?: string;
  /** File lane: what the host's own reader did with the file (PDF, Office…)
   * — its text rows, their embed job, or why it could not read it. */
  read?: {
    ok?: boolean;
    chunks?: number;
    reader?: string;
    embed_job_id?: number | null;
    embed_not_queued?: string | null;
    error?: string;
    note?: string;
  } | null;
}

// Seal a credential the composer caught into the vault (POST /api/vault/secrets,
// write:vault). Same call the Keys app makes; a same-name entry is superseded —
// unless `ifAbsent`, which the ask card's credential field sets: the ask named
// a NEW key, and a key of that name that appeared since is refused (409), never
// replaced (as the Circles Tickets pane does).
export function storeVaultSecret(
  name: string,
  credType: "api_key" | "opaque",
  value: string,
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

/** Raw-body upload to the brain's context intake (the same lane the Context
 * app uses: text/CSV chunks + embeds, binaries store as-is). Tags ride the
 * X-Tags header — the Workbench sends project:<id> so the chunks group as
 * that project's collection. Mirrors request()'s 401 retry (app-token race). */
export async function uploadContextFile(
  file: File,
  tags: string[] = [],
  _attempt = 0,
): Promise<ApiResult<UploadResult>> {
  try {
    const headers: Record<string, string> = {
      Accept: "application/json",
      "X-Filename": encodeURIComponent(file.name),
    };
    if (tags.length) {
      headers["X-Tags"] = tags.map((t) => encodeURIComponent(t)).join(",");
    }
    const res = await fetch("/api/brain/context/upload", {
      method: "POST",
      body: file,
      headers,
    });
    if (res.status === 401 && _attempt < 3) {
      await _sleep(120 * (_attempt + 1));
      return uploadContextFile(file, tags, _attempt + 1);
    }
    if (!res.ok) {
      const detail = await extractDetail(res);
      return {
        ok: false,
        status: res.status,
        message: detail || res.statusText || `HTTP ${res.status}`,
      };
    }
    return { ok: true, data: (await res.json()) as UploadResult };
  } catch (e) {
    const err = e as Error;
    return { ok: false, status: 0, message: err.message || "network error" };
  }
}

export interface EmbedJob {
  id: number;
  status: string;
  total: number;
  embedded: number;
  failed: number;
}

export function getEmbedJob(id: number): Promise<ApiResult<EmbedJob>> {
  return getJSON<EmbedJob>(`/api/brain/embed/jobs/${id}`);
}

// ---- Projects ----

/** Every project in every group, most recent activity first. The Workbench
 * is the one caller that passes include_archived=true: the
 * book's Index keeps archived pages in their chapters, so one call returns
 * all four groups. The route's default (archived hidden)
 * is unchanged for everyone else, including the host's list_projects. The
 * cap is the route's 1000 — the viewer is built for hundreds of projects. */
export async function fetchProjects(): Promise<ApiResult<Project[]>> {
  const r = await getJSON<Project[] | { projects: Project[] }>(
    "/api/projects?limit=1000&include_archived=true",
  );
  if (!r.ok) return r;
  const list = Array.isArray(r.data) ? r.data : (r.data.projects ?? []);
  return { ok: true, data: list };
}

/** The person has this project's page open in the book (PUT
 * /api/projects/{id}/seen, server/routes/projects.py mark_project_seen).
 * Answers the mark as stored, which never moves back. `turnToken`: the page's
 * turn the person watched end; its reply then never lights the page's tab,
 * however late its row is written (projects.seen_turns, v217). */
export function markProjectSeen(
  id: number,
  turnToken?: string | null,
): Promise<ApiResult<{ project_id: number; seen_at: string }>> {
  return turnToken
    ? request("PUT", `/api/projects/${id}/seen`, { turn_token: turnToken })
    : request("PUT", `/api/projects/${id}/seen`);
}

/** What the book shows at the top of a page: where the work stands, and the
 * page's marks (GET /api/projects/{id}/page, projects.py get_project_page;
 * read:brain_memory and read:inference_log, both held by the Workbench). */
export function fetchProjectPage(id: number): Promise<ApiResult<ProjectPage>> {
  return getJSON<ProjectPage>(`/api/projects/${id}/page`);
}

/** Archive a project. `deleteRepo` also deletes its Code Repository — only
 * after the person confirmed it, and the server refuses it wherever it is not
 * allowed (not the Host's, or another project uses it). */
export function archiveProject(
  id: number,
  deleteRepo = false,
): Promise<
  ApiResult<{ archived: boolean; repo_deleted?: boolean; repo?: string; repo_error?: string }>
> {
  return request("DELETE", `/api/projects/${id}${deleteRepo ? "?delete_repo=true" : ""}`);
}

export function restoreProject(
  id: number,
): Promise<ApiResult<{ updated: boolean }>> {
  return request("PUT", `/api/projects/${id}`, { status: "active" });
}

/** A page filed in a chapter: the project lands in `bucketId` (null = no
 * chapter) in front of `beforeId` (null = the end). One request; the server renumbers the
 * bucket and answers with its new order. */
export function placeProject(
  id: number,
  bucketId: number | null,
  beforeId: number | null,
): Promise<ApiResult<{ placed: boolean; bucket_id: number | null; order: number[] }>> {
  return request("PATCH", `/api/projects/${id}/place`, {
    bucket_id: bucketId,
    before_id: beforeId,
  });
}

// ---- Buckets (the v130 folders — GET/POST/PATCH/DELETE /api/projects/buckets) ----

export interface BucketsResult {
  buckets: Bucket[];
  unfiled: { counts: NonNullable<Bucket["counts"]> };
}

export function fetchBuckets(): Promise<ApiResult<BucketsResult>> {
  return getJSON<BucketsResult>("/api/projects/buckets");
}

export function createBucket(
  name: string,
  color?: string | null,
): Promise<ApiResult<Bucket>> {
  return request("POST", "/api/projects/buckets", { name, ...(color ? { color } : {}) });
}

export function updateBucket(
  id: number,
  patch: { name?: string; color?: string | null; sort_order?: number },
): Promise<ApiResult<{ updated: boolean }>> {
  return request("PATCH", `/api/projects/buckets/${id}`, patch);
}

/** Deletes the bucket only; its projects come back unfiled (the count says
 * how many), never deleted. */
export function deleteBucket(
  id: number,
): Promise<ApiResult<{ deleted: boolean; unfiled: number }>> {
  return request("DELETE", `/api/projects/buckets/${id}`);
}

// ---- The book's Index (server/routes/workbench_index.py) ----

/** The pages in the order they fit `q`, open and archived, each with what it
 * matched (POST /api/projects/index; no score). No model call. The words go
 * in the body and never in a URL, which whatever sits in front of the pod may
 * keep, and a secret typed a character at a time would reach it a prefix at
 * a time. They have their secret shapes taken out first (indexQueryText),
 * since a body still reaches the server's embedding call and the server cuts
 * hard shapes only. */
export function searchIndex(q: string, limit = 20): Promise<ApiResult<IndexAnswer>> {
  return request<IndexAnswer>("POST", "/api/projects/index", { q: indexQueryText(q), limit });
}

/** Enter in the Index's "send" mode (POST /api/workbench/index/send): the
 * Host picks the page, an archived pick is restored, and the turn starts
 * there. Answers the route's status and body as they are, a 409's
 * {detail: {state, reason}} included (lib/book-index.ts sendOutcome reads
 * them), which request() would fold into a message. `requestId` is this
 * message's: the server runs a send once per id. The pick is one model call,
 * so it may take a while. */
export async function sendFromIndex(
  text: string,
  currentProjectId: number | null,
  requestId: string,
  _attempt = 0,
): Promise<{ status: number; body: unknown }> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 120_000);
  try {
    const res = await fetch("/api/workbench/index/send", {
      method: "POST",
      signal: ctrl.signal,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ text, current_project_id: currentProjectId, request_id: requestId }),
    });
    // The app-token race, as request() handles it (app-web-kit/api.ts).
    if (res.status === 401 && _attempt < 3) {
      clearTimeout(timer);
      await _sleep(120 * (_attempt + 1));
      return sendFromIndex(text, currentProjectId, requestId, _attempt + 1);
    }
    let body: unknown = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    return { status: res.status, body };
  } catch (e) {
    const err = e as Error;
    return { status: 0, body: { detail: err.name === "AbortError" ? "timed out" : err.message || "network error" } };
  } finally {
    clearTimeout(timer);
  }
}

// ---- The project index (POST /api/projects/search) ----

export interface SearchResult {
  projects: SearchHit[];
  /** True when the host's embedding model ranked too; false = lexical only,
   * which is the normal state on a host with embeddings not set up. Never
   * an outage. */
  semantic: boolean;
  q: string;
}

/** The Start box's search as the person types. The words go in the body, not
 * a URL (see searchIndex). */
export function searchProjects(
  q: string,
  limit = 5,
): Promise<ApiResult<SearchResult>> {
  return request<SearchResult>("POST", "/api/projects/search", { q, limit });
}

/** Move the old General (projectless Workbench) thread into an EXISTING
 * project — the same retag-never-delete path creation-time adoption uses.
 * Only the first open after the General thread was retired needs it. */
export function adoptGeneralInto(
  projectId: number,
): Promise<ApiResult<AdoptedResult & { project_id: number }>> {
  return request("POST", `/api/projects/${projectId}/adopt-thread`, {
    kind: "app",
    ref: "workbench",
  });
}

export interface AdoptedResult {
  moved_turns?: number;
  moved_memories?: number;
  error?: string;
}

export function createProject(
  name: string,
  description = "",
  /** Move a chat thread into the new project at creation (the general→project
   * flow). The conversation is retagged, never deleted; a marker turn stays
   * behind in the source thread. */
  adoptThread?: { kind: string; ref: string; since?: string; until?: string },
  /** The Code Repository the project mounts (owner/name), or a fork of it.
   * Omitted, the Host makes a new one. */
  repository?: { repo: string; fork?: boolean },
): Promise<
  ApiResult<{
    id: number;
    name: string;
    status: string;
    repo?: string | null;
    repo_note?: string | null;
    forked_from?: string;
    adopted?: AdoptedResult;
  }>
> {
  return request(
    "POST",
    "/api/projects",
    {
      name,
      description,
      ...(adoptThread ? { adopt_thread: adoptThread } : {}),
      ...(repository ? { repo: repository.repo, fork: repository.fork === true } : {}),
    },
    // Mounting or forking reads the repository's code into the project.
    repository ? 300000 : 30000,
  );
}

/** Every Code Repository the Host can reach — what "start a project" offers. */
export function fetchCodeRepositories(): Promise<
  ApiResult<{ connected: boolean; host?: string; repositories: CodeRepository[] }>
> {
  return getJSON("/api/projects/code-repositories");
}

/** The open project's Code Repository, and what the Host may do there. */
export function fetchProjectRepository(
  projectId: number,
): Promise<ApiResult<ProjectRepository>> {
  return getJSON(`/api/projects/${projectId}/repository`);
}

// ---- Host conversation (the unified chat log, project-scoped) ----

export interface TurnRow {
  /** entity_memory turn-row id — the STABLE identity of the turn. The pane
   * keys React rendering on it; without it every refetch remounts the whole
   * thread (the "conversation reloads between responses" bug). */
  id?: number;
  role: "user" | "assistant";
  content: string;
  created_at?: string;
  /** Full turn-row metadata from /api/brain/chat — carries the per-turn
   * metrics stamp (gen_ms, tokens_*, model) on assistant rows, and the
   * persisted tool_calls chips (session.py _tc_meta). */
  metadata?: Record<string, unknown>;
}

/** Persisted tool-call chip as session.py's _tc_meta writes it into the turn
 * row's metadata: {tool, args_summary, status, error, kind?, task_id?}. */
export interface PersistedToolChip {
  tool?: string;
  args_summary?: string;
  /** The op/action the tool ran (session.py _tc_meta `op`). */
  op?: string;
  status?: string;
  error?: string | null;
  kind?: string;
  task_id?: string | number;
  /** review.fan_out chips: the fan-out session id. */
  session_id?: string | null;
  /** The inner tool of an `operate` call (session.py tool_call_chip). */
  operate_tool?: string;
  /** The result record the chip kept (runtime/tool_trail.py digest_tool_action). */
  digest?: Record<string, unknown>;
}

/** Rehydrate a persisted chip into the live ToolInvocation shape so tool and
 * agent cards survive a reload instead of vanishing from history (mirrors
 * circles-web's mapToolCall for the persisted-chip arm). */
export function mapPersistedToolChip(
  raw: PersistedToolChip,
  index: number,
): import("./types").ToolInvocation {
  const errored =
    raw.status === "error" || raw.status === "denied" || raw.status === "blocked";
  return {
    id: `chip-${index}`,
    name: raw.tool || "tool",
    args: {},
    summary: raw.args_summary || "",
    verb: (raw.op || "").replace(/[_.]/g, " ").toLowerCase(),
    result: raw.error ? { status: "error", result: raw.error } : { status: raw.status ?? "ok" },
    state: errored ? "output-error" : "output-available",
    kind: raw.kind === "agent" || raw.tool === "dispatch" ? "agent" : "tool",
    taskId: raw.task_id != null ? String(raw.task_id) : undefined,
    sessionId: raw.session_id ? String(raw.session_id) : undefined,
    operateTool: typeof raw.operate_tool === "string" ? raw.operate_tool : undefined,
    digest: raw.digest && typeof raw.digest === "object" ? raw.digest : undefined,
  };
}

export async function fetchHistory(
  projectId: number | null,
  sinceISO?: string,
): Promise<ApiResult<{ turns: TurnRow[] }>> {
  // INVARIANT: history windows are NEWEST-first (order=desc + limit), reversed
  // for chronological display. asc+limit returns the OLDEST N and HIDES recent
  // turns once a thread exceeds the window — the "my conversation disappeared"
  // bug: the general workbench thread has >60 lifetime turns, so with the
  // route's default order=asc the last 60 were months-old rows (ids ~5344)
  // while the live conversation (ids 6425+) never rendered after a reload.
  // Applies to BOTH the general (app_id=workbench) and project paths.
  const q = projectId != null ? `project_id=${projectId}` : "app_id=workbench";
  // `since` (inclusive) turns a refresh into a DELTA fetch — the delivery
  // poll passes the newest known turn's created_at so each tick moves a few
  // rows, not the whole window. The merge dedups the boundary row by id.
  const sinceQ = sinceISO ? `&since=${encodeURIComponent(sinceISO)}` : "";
  const r = await getJSON<{ turns: TurnRow[] }>(
    `/api/brain/chat?${q}&limit=60&order=desc${sinceQ}`,
  );
  if (!r.ok) return r;
  return { ok: true, data: { turns: [...(r.data.turns ?? [])].reverse() } };
}

// ---- Conversation depth (GET/PUT /api/brain/thread-context) ----
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
  };
}

/** projectId null = the General bench thread (app_id=workbench only). */
export async function fetchThreadContext(
  projectId: number | null,
): Promise<ApiResult<ThreadContextInfo>> {
  const q = projectId != null ? `&project_id=${projectId}` : "";
  const r = await getJSON<unknown>(`/api/brain/thread-context?app_id=workbench${q}`);
  if (!r.ok) return r;
  return { ok: true, data: normalizeThreadContext(r.data) };
}

/** Set (or clear, budget=null → server-derived default) the thread's token
 * budget. The depth bar's marker and the next turn's trim follow it. */
export function putThreadContextBudget(
  projectId: number | null,
  budget: number | null,
): Promise<ApiResult<{ ok?: boolean; budget?: number | null }>> {
  return request("PUT", "/api/brain/thread-context", {
    app_id: "workbench",
    project_id: projectId,
    budget,
  });
}

/** Set (or clear, null) what one turn on this thread may spend, in dollars.
 * Sent on its own: the endpoint keys each field on presence, so this leaves
 * the context budget exactly as it was. */
export function putThreadTurnAllowance(
  projectId: number | null,
  usd: number | null,
): Promise<ApiResult<{ ok?: boolean; turn_allowance_usd?: number | null }>> {
  return request("PUT", "/api/brain/thread-context", {
    app_id: "workbench",
    project_id: projectId,
    turn_allowance_usd: usd,
  });
}

// ---- Model catalog (same /api/inference/catalog circles-web reads) ----
// The depth bar's window fallback: the last turn's metrics.model resolved
// against the catalog's context_length when no thread-context route exists.

interface CatalogRawModel {
  id: string;
  name?: string;
  context_length?: number | null;
}

export async function fetchModelCatalog(): Promise<ApiResult<CatalogModel[]>> {
  const r = await getJSON<{
    local_models?: CatalogRawModel[];
    openrouter_models?: CatalogRawModel[];
  }>("/api/inference/catalog");
  if (!r.ok) return r;
  const local = r.data.local_models ?? [];
  const remote = r.data.openrouter_models ?? [];
  const models: CatalogModel[] = [
    ...local.map((m) => ({
      id: m.id,
      name: `${m.name || m.id} (local)`,
      provider: "local",
      contextLength: m.context_length ?? null,
    })),
    ...remote.map((m) => ({
      id: m.id,
      name: m.name || m.id,
      provider: "privatae",
      contextLength: m.context_length ?? null,
    })),
  ];
  return { ok: true, data: models };
}

/** The reasoning efforts this Host's turns may run at: the levels of
 * the Host model route's model and the level that runs when none is picked
 * (server/routes/inference.py effort_options). */
export function fetchEffortOptions(): Promise<ApiResult<EffortOptions>> {
  return getJSON<EffortOptions>("/api/inference/effort-options");
}

/** POST /api/brain/messages — the no-sessions brain path. app_id rides the
 * app token, so the host answers under the WORKBENCH surface prompt; the
 * project_id scopes the thread and tags the memory turns. `effort` is the
 * thread's reasoning-effort pick, sent only when one is picked; the route
 * checks it against the Host model and refuses one it does not take. */
export async function sendMessageStream(
  projectId: number | null,
  message: string,
  signal: AbortSignal,
  effort?: string | null,
  clientId?: string,
): Promise<Response> {
  // `clientId`: the words' id, the same on the send's silent retry as on its
  // first attempt, so a retry whose words the server already holds
  // (pending_words) never delivers them a second time.
  const body = JSON.stringify({
    message,
    project_id: projectId,
    ...(effort ? { effort } : {}),
    ...(clientId ? { client_id: clientId } : {}),
  });
  const hdrs = {
    "Content-Type": "application/json",
    Accept: "text/x-vercel-ai-data-stream",
  };
  let res = await fetch("/api/brain/messages", { method: "POST", headers: hdrs, body, signal });
  for (let a = 0; res.status === 401 && a < 3; a++) {
    await _sleep(120 * (a + 1)); // token-delivery race (see request())
    res = await fetch("/api/brain/messages", { method: "POST", headers: hdrs, body, signal });
  }
  return res;
}

// ---- Detached turns (GET/POST /api/brain/turns/*) ----
// The turn registry surface: see the running turn for a thread, reattach to
// its stream after a disconnect, and stop it server-side for real.

export function fetchActiveTurn(
  projectId: number | null,
): Promise<ApiResult<{ ok: boolean; turn: ActiveTurn | null; turns: ActiveTurn[] }>> {
  const qs = projectId == null ? "" : `?project_id=${projectId}`;
  return getJSON<{ ok: boolean; turn: ActiveTurn | null; turns: ActiveTurn[] }>(
    `/api/brain/turns/active${qs}`,
  );
}

/** Reattach to a turn's frame stream: replays buffered frames from `fromSeq`
 * (0 = the whole turn) then follows live. Same wire format as
 * sendMessageStream — consumeStream parses it unchanged. */
export function openTurnStream(
  turnId: string,
  fromSeq: number,
  signal: AbortSignal,
): Promise<Response> {
  return fetch(
    `/api/brain/turns/${encodeURIComponent(turnId)}/stream?from_seq=${fromSeq}`,
    { headers: { Accept: "text/x-vercel-ai-data-stream" }, signal },
  );
}

/** The real stop: cancels the DETACHED turn server-side (tears down the
 * engine and its subprocess), not just the local fetch. */
/** The person's words into the running turn (the composer's Send while the
 * turn holds). 409 when the turn has ended — the caller's held copy then
 * goes as the next message. `held`: the server has written the words down
 * (pending_words) and owns them from here — the turn reads them, or they
 * start the thread's next turn, after a restart too — so the page keeps no
 * copy. False in PII mode, where nothing is written: the page keeps its
 * copy until the turn's receipt. */
export function nudgeTurn(
  turnId: string,
  text: string,
  clientId?: string,
): Promise<ApiResult<{ ok: boolean; held?: boolean }>> {
  return request("POST", `/api/brain/turns/${encodeURIComponent(turnId)}/nudge`, {
    text,
    ...(clientId ? { client_id: clientId } : {}),
  });
}

/** Words the server holds for one page (GET /api/brain/pending): sent into
 * a running turn and not yet read by a turn. Shown as sent and waiting,
 * after a reload too. */
export interface HeldWords {
  id: string;
  text: string;
  client_id: string | null;
  received_at: string | null;
}
export function fetchPendingWords(
  projectId: number | null,
): Promise<ApiResult<{ ok: boolean; words: HeldWords[] }>> {
  const qs = projectId == null ? "" : `?project_id=${projectId}`;
  return getJSON<{ ok: boolean; words: HeldWords[] }>(`/api/brain/pending${qs}`);
}

/** One run the kill stopped (review.py kill_session_runs). `other_session`:
 * a run of another session that shared the engine's pod and stopped with it.
 * `not_started`: stopped before it reached its pod (waking it, waiting for a
 * slot, cloning in) — it never runs. */
export interface KilledRun {
  harness: string;
  session_id: string | null;
  label: string;
  other_session: boolean;
  not_started?: boolean;
}

export interface KillResult {
  ok: boolean;
  session_id: string;
  stopped: KilledRun[];
  errors: { harness: string; error: string }[];
  detail?: string;
  halted_project_id?: number | null;
}

/** The run's Kill: end a running build or compare on its engine's pod now.
 * Every other run sharing that pod stops too (named in `stopped`); a factory
 * build's project stops auto-advancing. Nothing running is an ordinary
 * answer — the run may have ended a moment before. */
export function killRun(sessionId: string, harness?: string): Promise<ApiResult<KillResult>> {
  return request(
    "POST",
    `/api/review/sessions/${encodeURIComponent(sessionId)}/kill`,
    harness ? { harness } : {},
  );
}

export function cancelTurn(
  turnId: string,
): Promise<ApiResult<{ ok: boolean; status: string }>> {
  return request("POST", `/api/brain/turns/${encodeURIComponent(turnId)}/cancel`);
}

// ---- The bench: the project lifecycle (plan / activity / result) ----

export function fetchPhases(
  projectId: number,
): Promise<ApiResult<{ phases: Phase[] }>> {
  return getJSON<{ phases: Phase[] }>(`/api/projects/${projectId}/phases`);
}

/** The user's signature on a phase (v124): approves a gated phase so it can
 * build.
 * The ONLY writer of approved_at — no host tool can. */
export function approvePhase(
  projectId: number,
  phaseId: number,
): Promise<
  ApiResult<{
    phase_id: number;
    status: string;
    approved: boolean;
    note?: string;
    project_complete?: boolean;
  }>
> {
  return request("POST", `/api/projects/${projectId}/phases/${phaseId}/approve`);
}

export function fetchPhaseSteps(
  projectId: number,
  phaseId: number,
): Promise<ApiResult<{ steps: PhaseStep[] }>> {
  return getJSON<{ steps: PhaseStep[] }>(
    `/api/projects/${projectId}/phases/${phaseId}/steps`,
  );
}


/** Self-contained preview HTML for one harness variant of a coding run
 * (review.preview_harness_index — "the bake-off iframe srcdoc source").
 * Fetched (not iframe src) so the app token rides the request; rendered via
 * iframe srcdoc. Same 401 token-race retry as request(). */
export async function fetchHarnessPreview(
  sessionId: string,
  harness: string,
): Promise<{ ok: boolean; html: string; message?: string }> {
  const url = `/api/review/sessions/${encodeURIComponent(sessionId)}/preview/${encodeURIComponent(harness)}`;
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      const res = await fetch(url, { cache: "no-store" });
      if (res.status === 401 && attempt < 3) {
        await _sleep(120 * (attempt + 1));
        continue;
      }
      if (!res.ok) {
        return {
          ok: false,
          html: "",
          message:
            res.status === 404
              ? "no renderable index.html in this variant's output"
              : `HTTP ${res.status}`,
        };
      }
      return { ok: true, html: await res.text() };
    } catch (e) {
      return { ok: false, html: "", message: (e as Error).message || "network error" };
    }
  }
  return { ok: false, html: "", message: "unauthorized" };
}

// ---- Modules and versions ----

export interface ModulesResult {
  project_id: number;
  modules: ModuleRow[];
  /** The app at the project root — the app of a project with no modules. */
  root_app?: AppState | null;
}

export function fetchModules(projectId: number): Promise<ApiResult<ModulesResult>> {
  return getJSON<ModulesResult>(`/api/projects/${projectId}/modules`);
}

export function fetchModuleVersions(
  projectId: number,
  module: string,
): Promise<ApiResult<{ versions: ModuleVersion[] }>> {
  return getJSON(
    `/api/projects/${projectId}/modules/${encodeURIComponent(module)}/versions`,
  );
}

/** Make version N the module's current one — the pointer moves, the module
 * directory becomes that version's snapshot, nothing is deleted. */
export function setModuleVersion(
  projectId: number,
  module: string,
  number: number,
): Promise<ApiResult<{ number: number; backup?: string | null }>> {
  return request(
    "POST",
    `/api/projects/${projectId}/modules/${encodeURIComponent(module)}/current`,
    { number },
    60000,
  );
}

/** Undo the module's current version (pointer back one; the version it
 * leaves stays and can be made current again). */
export function undoModuleVersion(
  projectId: number,
  module: string,
): Promise<ApiResult<{ number: number | null; from: number }>> {
  return request(
    "POST",
    `/api/projects/${projectId}/modules/${encodeURIComponent(module)}/undo`,
    undefined,
    60000,
  );
}

/** The user's pick: land run N in a module as its next version (review.adopt
 * into=module — the files as the run left them, reversible). */
export function adoptRunInto(
  runId: number,
  module: string,
): Promise<ApiResult<{ ok: boolean; version?: { number: number } }>> {
  return request("POST", "/api/review/adopt", { run_id: runId, into: module }, 120000);
}

// ---- The bench: coding runs + dispatched agents ----

export function fetchRunSessions(
  projectId: number | null,
): Promise<ApiResult<{ sessions: RunSession[] }>> {
  const q = projectId != null ? `&project_id=${projectId}` : "";
  return getJSON<{ sessions: RunSession[] }>(`/api/review/sessions?limit=20${q}`);
}

export function fetchRunDetail(sid: string): Promise<ApiResult<RunDetail>> {
  return getJSON<RunDetail>(`/api/review/sessions/${encodeURIComponent(sid)}`);
}

/** The numbered runs log (server/routes/compute.py::runs). Already reachable
 *  from here — the Workbench holds read:inference_log (app.yaml). The window
 *  is generous because the point of a NUMBER is that yesterday's run is still
 *  there to be named; the list is capped, not the history. */
export function fetchBenchRuns(
  projectId: number | null,
  limit = 40,
): Promise<ApiResult<{ runs: BenchRun[] }>> {
  const q = projectId != null ? `&project_id=${projectId}` : "";
  return getJSON<{ runs: BenchRun[] }>(
    `/api/compute/runs?window=90d&limit=${limit}${q}`,
  );
}

export function fetchTranscript(
  sid: string,
  harness: string,
): Promise<
  ApiResult<{
    available: boolean;
    transcript?: string;
    model?: string;
    elapsed_seconds?: number;
    exit_code?: number | null;
    error?: string;
  }>
> {
  return getJSON(
    `/api/review/sessions/${encodeURIComponent(sid)}/transcript/${encodeURIComponent(harness)}`,
  );
}

// ---- The Ladder (server/routes/ladder.py — climb runs) ----


export function fetchAgents(
  activeOnly = false,
): Promise<ApiResult<{ agents: AgentRow[] }>> {
  // limit=100 (route default is 20): the newest-20 window silently dropped
  // older still-running agents, freezing their status cards forever.
  return getJSON<{ agents: AgentRow[] }>(
    `/api/agents?limit=100${activeOnly ? "&active_only=true" : ""}`,
  );
}

/** A timer the host set for itself: a scheduled task on the clock, with the
 * moment it fires, so the time is visible. It could already set one
 * (schedule_task → offline_task_runner); the pending timer was visible only
 * in the Agents app, so the bench you watch a build on showed nothing. */
export type ScheduledTimer = {
  event_id: string;
  title: string;
  lane: "op" | "turn";
  recurrence: string | null;
  status: string;
  project_id: number | null;
  next_fire_at: string | null;
};

export function fetchScheduled(): Promise<ApiResult<{ scheduled: ScheduledTimer[] }>> {
  return getJSON<{ scheduled: ScheduledTimer[] }>("/api/agents/scheduled");
}

/** Cancel a timer — the same off switch the Agents app uses. */
export function cancelScheduled(
  eventId: string,
): Promise<ApiResult<{ event_id: string; status: string }>> {
  return request(
    "POST",
    `/api/agents/scheduled/${encodeURIComponent(eventId)}/cancel`,
  );
}

/** Kill a running background agent — same endpoint the Circles app uses. */
export function cancelAgent(
  taskId: string | number,
): Promise<ApiResult<{ task_id: number; status: string }>> {
  return request(
    "POST",
    `/api/agents/${encodeURIComponent(String(taskId))}/cancel`,
  );
}

// ---- Publish as site (the public webapp zone) ----

export interface SitePublishResult {
  hostname: string;
  site_id: number;
  version_id: number;
  runtime: string;
  status: string;
  staging_note?: string;
}

/** One-click publish: the kernel zips the project's built web output
 * server-side and routes it through the caller-scoped publish op — the user
 * never handles a zip. A new site goes live public. */
export function publishProjectAsSite(
  projectId: number,
  slug: string,
): Promise<ApiResult<SitePublishResult>> {
  return request(
    "POST",
    "/api/applications/sites/publish-project",
    { project_id: projectId, slug },
    120000, // server-side zip + bridge + controller round-trip
  );
}

export interface InstalledAppResult {
  id: string;
  name?: string;
  version?: string;
  status?: string;
}

/** Disposition "Install as my app": packs the completed project into a signed
 * .cseries bundle and self-installs it (provenance=custom). Wires the EXISTING
 * POST /api/applications/build {project_id} route (build_from_project /
 * ship_project_service). The project must be status='complete'. */
export function buildFromProject(
  projectId: number,
): Promise<ApiResult<InstalledAppResult>> {
  return request(
    "POST",
    "/api/applications/build",
    { project_id: projectId },
    120000, // pack + sign + install
  );
}

// ---- Code Approvals (server/routes/workbench.py) ----
// The open project's pull requests, in plain English. Nothing here merges: the
// person approves in a window on the platform's own site (approve_url), and the
// Host merges only under Auto-Approve.

export function fetchApprovals(projectId: number): Promise<
  ApiResult<{
    connected: boolean;
    repo?: string;
    on_machine?: boolean;
    machine?: string | null;
    repo_url?: string;
    approvals: CodeApproval[];
  }>
> {
  return getJSON(`/api/workbench/approvals?project_id=${projectId}`);
}

export function fetchApproval(
  projectId: number,
  number: number,
): Promise<ApiResult<CodeApprovalDetail>> {
  return getJSON(`/api/workbench/approvals/${number}?project_id=${projectId}`);
}

export function fetchApprovalDiff(
  projectId: number,
  number: number,
): Promise<ApiResult<{ diff: string }>> {
  return getJSON(`/api/workbench/approvals/${number}/diff?project_id=${projectId}`);
}

/** Record "changes asked for"; returns the message for the Host. */
export function askForChanges(
  projectId: number,
  number: number,
  note: string,
): Promise<ApiResult<{ recorded: boolean; message: string }>> {
  return request("POST", `/api/workbench/approvals/${number}/changes`, {
    project_id: projectId,
    note,
  });
}

/** Write the chat line for a merged Code Approval (once; a no-op otherwise). */
export function announceApproval(
  projectId: number,
  number: number,
): Promise<ApiResult<{ announced: boolean; text?: string; already?: boolean }>> {
  return request("POST", `/api/workbench/approvals/${number}/announce`, {
    project_id: projectId,
  });
}

// ---- Auto-Approve (per-project; config key auto_merge) ----
// One rolling Code Approval per project, each phase a commit inside it; with
// this on, the Host approves and merges it itself when the LAST phase of the
// approved plan lands clean (server/routes/projects.py get/set_auto_merge,
// applied in tools.py _run_phase_build). Off by default — otherwise the person
// approves. Offered only where the Host can merge (ProjectRepository).

export function setAutoMerge(
  projectId: number,
  enabled: boolean,
): Promise<ApiResult<{ auto_merge: boolean }>> {
  return request("PUT", `/api/projects/${projectId}/auto-merge`, { enabled });
}
