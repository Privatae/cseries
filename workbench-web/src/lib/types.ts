// Domain model for the Workbench app. Deliberately independent of the `ai`
// package's UIMessage type — same convention as circles-web.

export type ChatStatus = "ready" | "submitted" | "streaming" | "error";

export type ToolState = "input-available" | "output-available" | "output-error";

export interface ToolInvocation {
  id: string; // toolCallId
  name: string; // toolName
  args: unknown;
  summary: string; // one-line summary for the collapsed header
  /** The op/action the tool ran, as words — the header reads "<Tool> <verb>";
   * the summary is the quoted subject. */
  verb?: string;
  result?: unknown;
  state: ToolState;
  /** "agent" when this invocation dispatched a background agent — work the
   * host DELEGATED, rendered distinctly from its own tool calls. */
  kind?: "tool" | "agent";
  taskId?: string;
  /** review.fan_out chips: the fan-out session id (persisted on the chip
   * since workbench 0.13.3 so one terminal per harness mounts on reload). */
  sessionId?: string;
  /** Persisted chips only: the inner tool of an `operate` call
   * (session.py tool_call_chip `operate_tool`). */
  operateTool?: string;
  /** Persisted chips only: the bounded result record the chip kept
   * (runtime/tool_trail.py digest — ids, preview_url, status). Where a
   * reloaded artifact card finds its artifact (app-web-kit/visuals). */
  digest?: Record<string, unknown>;
}

/** Per-turn run metrics — stamped into the turn row's metadata by the
 * runtime at write (session.py), or measured client-side for the turn
 * that just streamed. All fields optional: old rows predate the stamp. */
export interface TurnMetrics {
  gen_ms?: number | null;
  tokens_total?: number | null;
  tokens_in?: number | null;
  tokens_out?: number | null;
  /** Prefix tokens served FROM cache this turn — a SUBSET of tokens_in, not an
   * addition to it. Shown so the user can tell what they actually consumed
   * from what the cache served, which on a subscription is the difference
   * between burning a refresh window and coasting through it. */
  tokens_cached?: number | null;
  /** Prefix tokens WRITTEN to cache this turn (also a subset of tokens_in). */
  tokens_cache_write?: number | null;
  /** Provenance — WHO ANSWERED. Printed under every assistant turn, so nobody
   * has to ask what model ran it. */
  model?: string | null;
  provider?: string | null;
  /** Set when the primary errored and the route's fallback provider took
   * the call — a silent downgrade must be readable on the turn. */
  failed_over?: import("./stream").FailedOver | null;
  /** What the turn actually sent — per model call, history, tools, results
   * (sizes only). The answer to "what context was used?" on the turn. */
  context_ledger?: import("./stream").ContextLedger | null;
  /** The Host's close_turn signal for this turn (the Review button). */
  close_turn?: import("@kit/close-turn").CloseTurnSignal | null;
  /** What the turn added to its page's running count: its spend record's
   * tokens summed over every call (metadata.context_ledger.spend.tokens, the
   * same figure the page's total sums, context_budget.running_spend). Null on
   * a row from before the record rode the turn. */
  spend_tokens?: number | null;
  /** The turn's list-price estimate (metadata.est_cost_usd) and the word that
   * stands where no price does (price_basis: free, subscription, unpriced,
   * partial). */
  est_cost_usd?: number | null;
  price_basis?: string | null;
  /** The effort the answering call ran at (metadata.effort.sent,
   * inference_config.describe_effort); null when none was sent. */
  effort_sent?: string | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  tools: ToolInvocation[];
  createdAt: string;
  error?: string;
  streaming?: boolean;
  metrics?: TurnMetrics;
  /** Live reasoning feed (`g:` frames) — what the host is thinking RIGHT NOW.
   * Live-only; persisted history doesn't carry it. */
  reasoning?: string;
  /** `metadata.task_id` on a turn a background job delivered (session.py
   * deliver_agent_result `_agent_tags`) — the delivery poll's success test
   * when the trigger was the bench flip, which carries no row id. */
  agentTaskId?: string;
  /** `metadata.code_approval` on the line a Code Approval's merge wrote into
   * the thread (server/routes/code_approvals.py announce_merge) — what the
   * chat waits for after a merge. */
  codeApproval?: number;
  /** The turn that wrote it: from the `2:[{type:"session"}]` frame on a live
   * echo, from `metadata.turn_token` on a saved row. mergeHistory swaps an
   * echo for the saved row carrying the same token. */
  turnToken?: string;
  /** A message the person sent INTO a running turn (a nudge, the composer's
   * Send while the turn holds). Saved as a user row with metadata.nudge when
   * the turn reads it; the merge swaps the echo for that row, never for
   * another row with the same words. */
  nudge?: boolean;
  /** The running turn READ this nudge (its nudge_read receipt came): the words
   * reached the Host in that turn and will not go again as a message. A saved
   * nudge row was read by definition. */
  nudgeRead?: boolean;
  /** The id this nudge was sent with (POST .../nudge client_id): on the
   * line the composer drew, on a line a `nudge` frame drew (the frame's
   * client_id), and on the saved row (metadata.client_id). Lines, frames,
   * receipts and rows are matched by it (lib/pending-sends sameNudge). */
  nudgeId?: string;
  /** The person held Send to think harder: the level this message was sent
   * at (the book's hold-to-think). Local to the echo; the reply's own row
   * records the level it ran at (TurnMetrics.effort_sent). */
  heldEffort?: string;
}

/** One "a background job delivered its report into a thread" event.
 *
 * `rowId` is the entity_memory id the kernel wrote, carried on the /ws frame
 * (null on the bench-poll path and on an older kernel that doesn't send it);
 * `taskId` names the job; `projectId` null means unknown (a socket reopen,
 * which announces "something may have landed" without saying what).
 *
 * This replaces the bare nonce the pane used to receive. A nonce can only say
 * "refetch"; it cannot say WHAT to wait for, so the pane's only available
 * success test was "the thread grew" — and it always had. */
export interface DeliveryEvent {
  seq: number;
  projectId: number | null;
  taskId: string | null;
  rowId: number | null;
  /** The ORIGIN surface of the frame (`module` — session.py stamps the
   * dispatch's own app_id). A Circles-origin delivery is not ours to wait
   * for. Null on the bench path, which is the Workbench's own poll. */
  appId: string | null;
}

/** Merges whose chat lines the chat pane of that project waits for, until the
 * lines tagged with these numbers are on screen. A merge that lands while a
 * wait runs joins it, so no earlier line loses its wait. `seq` makes each
 * change a new event. */
export interface ApprovalLineEvent {
  seq: number;
  projectId: number;
  lines: {
    number: number;
    /** The raiser knows the line is written. When it only knows another
     * writer holds the write (an "already"), a wait that runs out stays
     * quiet about it. */
    written: boolean;
  }[];
}

/** One row of the inference catalog (server/routes/inference.py
 * get_model_catalog) — the depth bar resolves context_length from it. */
export interface CatalogModel {
  id: string;
  name: string;
  provider: string; // "privatae" | "local"
  /** Max context window in tokens, if known. */
  contextLength?: number | null;
}

/** A registered turn in the server's turn registry (GET /api/brain/turns/
 * active) — running or recently finished. The reattach + cancel handle. */
export interface ActiveTurn {
  turn_id: string;
  status: "running" | "done" | "error" | "cancelled" | "timeout";
  started_at: number;
  elapsed_s: number;
  user_message: string;
  session_id?: string | null;
  seq: number;
  /** The conversation the turn runs in (server/turn_registry.py
   * compose_thread_key): `project|<entity>|<project id>` for a project's
   * thread. The book reads which pages have the Host working from it. */
  thread_key?: string;
}

// ---- Projects (server/routes/projects.py) ----

/** A project's status, folded to the four words the Workbench shows —
 * server/routes/projects.py PROJECT_GROUP_OF_STATUS maps status onto them
 * (review folds into active). */
export type ProjectGroup = "active" | "pending" | "completed" | "archived";

export interface Project {
  id: number;
  name: string;
  description?: string;
  status?: string;
  /** Server-stamped group; lib/project-groups.ts derives it from
   * status when absent. */
  group?: ProjectGroup | string;
  /** The bucket it sits in (v130 folders, called buckets);
   * null/undefined = unfiled. */
  bucket_id?: number | null;
  /** Its place in the bucket (v173, PATCH /api/projects/{id}/place); null =
   * never placed — it sorts after the placed ones, newest activity first. */
  sort_order?: number | null;
  phase_count?: number;
  step_count?: number;
  steps_done?: number;
  steps_failed?: number;
  created_at?: string;
  updated_at?: string;
  /** Newest of: row update, last chat turn in the project's thread, last
   * build/fan-out against it (server-computed). The book orders by this. */
  last_activity_at?: string | null;
  /** owner/name of THIS project's gitea repo (projects.repo, v081 repo-per-
   * project). The list route has always selected it (projects.py:432); nothing
   * declared it here, so the PR pane fell back to "first repo on the whole
   * gitea" and showed another project's pull requests. Null when gitea wasn't
   * connected at creation — a build backfills it via ensure_project_repo. */
  repo?: string | null;
  /** The project's code lives on the user's own machine (the code server the
   * Host built there — pipeline source `machine-forge`). Its repo is reached
   * by the Host only: no link opens it, and its PRs are reviewed here. */
  code_on_machine?: boolean;
  /** The phase step being built right now, if any (list_projects). */
  step_in_flight_title?: string | null;
  // The page marks every list row carries for the book's tabs
  // (server/routes/projects.py _PAGE_MARKS_COLS, v215).
  /** When the person last had the project's page open in the book (PUT
   * /api/projects/{id}/seen). Null: never, since the mark has existed. */
  seen_at?: string | null;
  /** The newest moment the Host produced something here: a reply, a report
   * delivered into the thread, a task that finished. The person's own
   * messages never count. */
  last_landed_at?: string | null;
  /** What lights the page's tab: the newest of what landed after seen_at
   * that the person did not watch arrive (projects._PAGE_MARKS_COLS, slice
   * 3b): a reply whose turn the page marked as watched (PUT /seen
   * {turn_token}) never counts, however late its row is written. Null when
   * nothing has, and while seen_at is null. Absent from a server before it. */
  unseen_landed_at?: string | null;
  /** Open ask cards plus waiting Code Approvals. */
  waiting_on_you?: number;
  ask_cards_open?: number;
  /** The forge's count as last read (code_approvals.record_waiting); null
   * when it was never read. */
  approvals_waiting?: number | null;
  approvals_checked_at?: string | null;
}

/** The goal and the last turn's handoff (close_turn.py thread_state_parts),
 * and the conversation's running totals (context_budget.py running_spend):
 * the server's shapes for any conversation, the book's own
 * (app-web-kit/book). */
import type { WhereThisStands } from "@kit/book/page-top";
import type { RunningTotals } from "@kit/book/page-count";
export type { RunningTotals, WhereThisStands };

/** The page's coding-engine runs, the Compute app's own row for the project
 * (server/routes/compute.py project_run_usage). */
export interface EngineUsage {
  runs: number;
  /** Null when no run carried a token count. */
  tokens: number | null;
  /** The runs `tokens` covers. */
  tokens_covered_runs: number;
  billed_usd: number;
  est_compute_usd: number;
  privatae_usd: number;
  /** The vendor's own reported dollars, an estimate; null when no run
   * reported any. */
  vendor_est_usd: number | null;
  vendor_est_runs: number;
}

/** GET /api/projects/{id}/page (projects.py project_page_service). Each of
 * the three reads is best-effort and is null when it failed. */
export interface ProjectPage {
  project_id: number;
  where_this_stands: WhereThisStands | null;
  totals?: RunningTotals | null;
  engines?: EngineUsage | null;
  seen_at: string | null;
  last_landed_at: string | null;
  unseen_landed_at?: string | null;
  waiting_on_you: number;
  ask_cards_open: number;
  approvals_waiting: number | null;
  approvals_checked_at: string | null;
}

/** A bucket (the v130 project_folders rows): a chapter of the book's Index.
 * Flat; a project sits in one bucket or none. Counts arrive from GET
 * /api/projects/buckets. */
export interface Bucket {
  id: number;
  name: string;
  color?: string | null;
  sort_order: number;
  counts?: { active: number; pending: number; completed: number; archived: number; total: number };
}

/** One ranked row from POST /api/projects/search — a Project plus its score
 * (null on the empty-query "recent" state). */
export interface SearchHit extends Project {
  score: number | null;
}

// ---- Project plan (server/routes/projects.py phases/steps) ----

export interface Phase {
  id: number;
  phase_number: number;
  title: string;
  description?: string | null;
  status?: string; // planning | speccing | ready | in_progress | review | complete | failed | archived
  step_count?: number;
  steps_done?: number;
  due_at?: string | null;
  // v124 — the phase's gate. gate 'user' = not buildable until the
  // user approves it here; approved_at = when they did. acceptance_checks is
  // information only. 'review' status is legacy (old rows): shown as landed.
  gate?: "none" | "user" | string;
  approved_at?: string | null;
  acceptance_checks?: number;
  /** The module the phase builds; null = the project root. */
  module?: string | null;
  /** Optional and decides nothing: checks | proof | review. */
  validation?: "checks" | "proof" | "review" | string | null;
  // v175 — parallel builds. The phase NUMBERS it
  // waits for, by the scheduler's own rule; phases whose waits are complete
  // build together.
  waits_for?: number[];
  /** Its own engine, when the plan named one (null = the build's). */
  harness?: string | null;
  model?: string | null;
  /** Its last run collided with another phase's work and did not land. */
  integration_error?: string | null;
}

// ---- Modules and versions (server/routes/project_modules.py) ----

/** server.applications.app_state — what the platform OBSERVED about an app.
 * Every field is measured; none is inferred. */
export interface AppState {
  app_id: string | null;
  kind: string | null;
  installed: boolean;
  running: boolean;
  opens_via: "desktop" | null;
  install_action: string | null;
  manifest_error?: string | null;
}

/** `op`: a program the Host runs on one of the user's own
 * machines through their bastion, never an app. */
export type ModuleTarget = "private_app" | "public_site" | "store_app" | "component" | "op";

/** A finished run built FOR a module that is not one of its versions yet —
 * a compare's variant before the user takes one. `run_id` is the number in
 * the runs log. */
export interface VersionCandidate {
  run_id: number;
  session_id: string;
  harness: string;
  model: string | null;
  kind: string;
  at: string | null;
}

export interface ModuleRow {
  name: string;
  target: ModuleTarget | string;
  validation: "checks" | "proof" | "review" | string;
  description: string;
  current_version: {
    number: number;
    run_id: number | null;
    commit: string | null;
    source: string;
    at: string | null;
  } | null;
  version_count: number;
  candidates: VersionCandidate[];
  /** Observed app state for an installable target (private_app, store_app);
   * null for a site or a component. */
  app: AppState | null;
}

export interface ModuleVersion {
  number: number;
  run_id: number | null;
  commit: string | null;
  source: string; // build | adopt
  note: string;
  current: boolean;
  /** Its snapshot is still on disk (the newest 10 and the current are kept). */
  restorable: boolean;
  undone: boolean;
  at: string | null;
}

export interface PhaseStep {
  id: number;
  step_number: number;
  title: string;
  description?: string | null;
  status?: string; // pending | speccing | implementing | checking | done | failed
  domain_tag?: string | null;
  execute_model?: string | null;
  provenance_count?: number;
}

// ---- Live view (server/routes/projects.py /activity merged stream) ----

export interface ActivityEvent {
  kind: "tool_call" | "research";
  at: string;
  // kind=tool_call
  tool?: string;
  status?: string | null;
  error?: string | null;
  elapsed_ms?: number | null;
  // kind=research
  content?: string;
  source_kind?: string;
}

// ---- Coding runs (server/routes/review.py sessions) ----

export interface HarnessState {
  state?: string; // queued | running | done | failed
  /** "memory" while a queued variant waits for a running harness to release
   * (fan-out waves, review.py _apply_capacity); `queued_note` says why. */
  queued_for?: string | null;
  queued_note?: string;
  model?: string;
  error?: string;
  error_type?: string;
  elapsed?: number;
  elapsed_seconds?: number;
  diff_bytes?: number;
}

export interface RunSession {
  session_id: string;
  status: string; // queued | running | done | failed | partial
  prompt?: string;
  /** Epoch seconds: review.py writes the manifest's time.time(). */
  started_at?: number | string | null;
  completed_at?: number | string | null;
  /** The harnesses the run was sent to (manifest); per_harness names its
   * variants. */
  harnesses?: string[];
  project_id?: number | null;
  has_picks?: boolean;
  per_harness?: Record<string, HarnessState>;
  // The run's MoA arms, serialized off the dispatch manifest
  // (review.list_sessions_service): Self-MoA sample counts per harness,
  // the per-run vCPU size, the model-wait window (max_minutes), and whether worktrees were
  // seeded from existing project content (edit) or started empty.
  samples?: Record<string, number>;
  vcpus?: number | null;
  max_minutes?: number | null;
  seeded?: boolean | null;
}

/** One numbered run in the bench's runs log — a harness_run row (server:
 *  GET /api/compute/runs). ONE ROW PER (session_id, harness), which is the
 *  granularity that matters: a fan-out is N variants of one task, and the
 *  thing the user points at ("I like the design in run 14") is a single
 *  variant, not the task. `id` is harness_run.id — the number printed in the
 *  log and the whole address the host needs (harness_transcript takes
 *  run_id). Stable forever: curation strips transcripts, never rows. */
export interface BenchRun {
  id: number;
  session_id: string;
  project_id: number | null;
  project_name: string | null;
  harness: string;
  model: string | null;
  ok: boolean;
  status: string; // active | archived
  exit_code: number | null;
  has_error: boolean;
  diff_bytes: number;
  elapsed_s: number;
  created_at: string;
}

export interface RunDetail {
  session_id: string;
  status: string;
  manifest?: { prompt?: string; completed_at?: string };
  per_harness?: Record<string, HarnessState>;
  diffs?: Record<string, string>;
  picks?: Record<string, string>;
}

// ---- The Ladder (server/routes/ladder.py — climb runs) ----

/** One configured rung (ladder.py _valid_rung). harness "*" = the full bench. */
export interface LadderRung {
  harness: string;
  max_minutes?: number;
  samples?: number;
  model?: string | null;
  effort?: string | null;
}

/** Post-run transcript analysis for one variant (routes/_run_watchdog). */
export interface WatchdogStat {
  parsed?: boolean;
  verdict?: string | null;
  top_repeat?: string | null;
  max_consec_identical?: number;
}

/** One rung attempt (ladder.py _climb). per_harness rows reuse HarnessState
 * (same field subset the climb records). */
export interface LadderAttempt {
  rung: number;
  config: LadderRung;
  started_at?: number;
  session_id?: string | null;
  skipped?: { harness: string; reason: string }[];
  run_status?: string; // done | failed | partial | not-dispatched | unknown
  watchdog?: Record<string, WatchdogStat>;
  per_harness?: Record<string, HarnessState>;
  reasons?: Record<string, string>; // evidence-grade failure classification
  verify?: {
    cmd?: string;
    pass_at_k?: number | null;
    passed_variants?: (string | null)[];
  };
  evidence_out?: string;
}

/** GET /api/ladder/runs row (ladder.list_runs). */
export interface LadderRunRow {
  ladder_id: string;
  status: string; // running | awaiting_review | accepted | exhausted | error
  rung?: number; // next_rung
  rungs?: number;
  attempts?: number;
  session_ids?: string[];
  checked?: boolean; // has a deterministic test_cmd
  prompt?: string;
  created_at?: number;
  updated_at?: number;
}

/** GET /api/ladder/runs/{id} — the full climb state (ladder.get_run returns
 * state.json verbatim). */
export interface LadderRunDetail {
  ladder_id: string;
  status: string;
  next_rung: number;
  prompt?: string;
  project_dir?: string | null;
  project_id?: number | null;
  test_cmd?: string | null;
  rungs: LadderRung[];
  vcpus?: number | null;
  attempts: LadderAttempt[];
  evidence?: string;
  decision_note?: string;
  error?: string;
  created_at?: number;
  updated_at?: number;
}

// ---- Dispatched background agents (GET /api/agents) ----

/** One frame of the live tool feed AgentDispatch._activity_recorder persists
 * (rolling, capped) on the agent's registry row. */
export interface AgentActivity {
  at?: string;
  event?: "start" | "result";
  tool?: string;
  args?: string; // truncated preview (JSON string)
  result?: string; // truncated preview (JSON string)
  status?: string;
}

/** Shape = AgentDispatch._row_to_agent (brain/mastercode/runtime/agent_dispatch.py).
 * Registry statuses are entity_tasks statuses — a live agent is `in_progress`
 * (not "running"); the old output/failure keys never existed on this API. */
export interface AgentRow {
  task_id?: number | string;
  title?: string;
  /** 'agent' (a dispatched agent) | 'factory_build' (a build on the harness
   * pool) | 'fan_out' (one task across several harnesses on the pool). Pool
   * rows render distinctly and carry no veto — killing the registry row
   * would not stop the pool run. */
  task_type?: string;
  /** Factory builds: the project they build into (dispatched agents: null). */
  project_id?: number | null;
  /** WHERE this run's report was delivered — the surface and project from
   * `result.origin`, the same dict deliver_agent_result routes the turn by
   * (agent_dispatch._row_to_agent). A dispatched agent's scope lives ONLY
   * here; `project_id` above is null for it. Absent on older rows, which
   * is why the client treats "no origin" as "not mine".*/
  origin_app_id?: string | null;
  origin_project_id?: number | null;
  /** Factory builds: the coding harness the build runs/ran on (stamped when
   * the row opens; absent on older rows). */
  harness?: string | null;
  /** Factory builds: build-contract violations left after the fix loop. */
  violations?: string[];
  /** Factory builds: the save point this phase produced — the commit on
   * host/build and the rolling PR it landed in (project_git.sync_project_repo).
   * `error`/`note` when the commit or push did not happen. */
  repo_sync?: {
    synced?: boolean;
    repo?: string;
    committed?: boolean;
    commit?: string;
    pushed?: boolean;
    pr?: number | null;
    url?: string | null;
    ready?: boolean;
    error?: string | null;
    note?: string;
  } | null;
  /** Set only on the phase that completed the plan, with auto-merge on:
   * whether the host merged the PR, or the reason it declined to. */
  auto_merge?: {
    merged?: boolean;
    pr?: number | null;
    url?: string | null;
    reason?: string;
  } | null;
  status?: string; // queued | in_progress | completed | failed | cancelled
  created_at?: string;
  completed_at?: string | null;
  est_cost_usd?: number | null;
  tokens?: number | null;
  result?: string | null; // the agent's final output
  error?: string | null; // terminal failure text
  verdict?: string | null; // pass | fail | blocked | unavailable
  denied_tools?: string[]; // the tools a 'blocked' run could not honour
  /** One sentence from scoped_agent saying WHICH wall blocked it: a code-exec
   * tool stripped (code runs only on the harness pool), a call outside the grant, or a grant that never
   * covered the tool's capabilities. Absent on older records. */
  blocked_reason?: string | null;
  /** Cancelled rows: who killed it (user_veto | entity_tasks_tool | task_manage_api). */
  cancelled_by?: string | null;
  activity?: AgentActivity[];
  delivered_at?: string | null;
  hb_tool?: string | null;
  age_s?: number | null;
  tool_scope?: string[];
  [k: string]: unknown;
}

// ---- Code Repositories and Code Approvals (server/routes/projects.py +
// code_approvals.py) ----

/** A repository the Host can reach, as "start a project" offers it. */
export interface CodeRepository {
  full_name: string;
  owner: string;
  name: string;
  owned_by_host: boolean;
  can_write: boolean;
  private: boolean;
  empty: boolean;
  mirror: boolean;
  default_branch: string;
  description: string;
  updated_at?: string | null;
  /** The live project already on it — the fork offer's cue. */
  project: { id: number; name: string } | null;
  url?: string;
}

/** The open project's Code Repository (GET /api/projects/{id}/repository). */
export interface ProjectRepository {
  project_id: number;
  repo: string | null;
  on_machine: boolean;
  machine: string | null;
  url: string;
  owner: string | null;
  owned_by_host: boolean;
  default_branch: string | null;
  can_delete: boolean;
  delete_refusal: string | null;
  /** Auto-Approve is offered only where the Host can merge. */
  host_can_merge: boolean;
  auto_approve: boolean;
}

export type ApprovalState = "waiting" | "changes_asked" | "approved" | "closed";

/** One row of the Code Approvals list. */
export interface CodeApproval {
  number: number;
  title: string;
  summary: string;
  state: ApprovalState;
  state_words: string;
  ready: boolean;
  models: string[];
  plain: boolean;
  author?: string | null;
  merged_by?: string | null;
  merged_at?: string | null;
  updated_at?: string | null;
  head_sha?: string | null;
  base?: string | null;
  announced: boolean;
  /** The project's own approval (its work branch); a mounted repository also
   * lists everyone else's. */
  own: boolean;
  /** Merged since the project first listed its approvals and not yet in the
   * chat — the Workbench asks for its line (the server decides). */
  announce: boolean;
}

/** One Code Approval in full, and where the person approves it. */
export interface CodeApprovalDetail extends CodeApproval {
  repo: string;
  body: string;
  mergeable?: boolean | null;
  changed_files?: number | null;
  additions?: number | null;
  deletions?: number | null;
  on_machine: boolean;
  /** The platform's approve window; null where the platform cannot reach. */
  approve_url: string | null;
  approve_where: string | null;
  repo_url?: string;
}

/** A pool build in flight, reported from the bench poll to the chat pane.
 *
 * The chat mounts a run terminal off a `build` tool chip, so a phase the HOST
 * started on its own — approved-plan auto-advance, tools._autoadvance_start_phase
 * — has no chip and could never stream in the conversation ("when a
 * build action is going on it no longer shows the live feed in the chat stream
 * ... the only thing i see is live activity in the right pane"). This is how the
 * chat learns about one. */
export interface RunningBuild {
  /** entity_tasks.task_id — the run's session is `factory-build-<taskId>`. */
  taskId: string;
  title: string;
  harness?: string;
}
