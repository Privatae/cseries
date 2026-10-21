"""Meta-tool capability registry.

The platform's capability mask, per tool. Every tool the host or a dispatched
agent can call is authorized here, by `_authorize_meta_tool` (`runtime/tools.py`),
against `entity_bits & grant_bits`.

This registry began as the meta-tool half of a two-lane model: bots encoded
their mask in a binary-prefix FILENAME, meta-tools (having no filename) encoded
the same mask here. The bot lane is retired — its last live tool,
`context`, moved inline to `runtime/context_ops.py` and its bits moved into this
table — so this registry is now the ONLY place a capability mask is declared.
That is a simplification, not a loosening: same mask, same check, one lane
instead of two, and a declaration that ships inside the image rather than in a
separately-signed manifest.

AGENTS ARE BIT-CONFINED AT EXECUTION. A dispatched agent does not run at the
entity's FULL bits, and `tool_scope` is not the only filter (it narrows only
the advertised schema list, not execution). The agent GRANT closes that: the
host mints a grant whose floor is zero, `_execute_meta_tool` takes it as `grant_bits`, and the gate
resolves `effective_bits = entity_bits & grant_bits` before the required-bits
check. `scoped_agent` passes that grant on BOTH exec branches. So an agent IS
bit-confined at execution, not merely at advertisement. See `runtime/agent_grant.py`
for the rule and `scripts/test_agent_grant_wall.py` for the gate.

Architecture (an "interwoven firewall"):

  There are MULTIPLE CEIGAS domains, each running the SAME invariant
  invariants on a DIFFERENT scope vocabulary:

    * The operator's CEIGAS domain — governs platform-level
      scopes (publish:apps, write:billing, etc.). Bit changes here
      are the operator's, MFA-gated, OUTSIDE the entity pod.

    * Each entity's CEIGAS domain — governs that entity's own
      resources only (own memory, own sessions, own projects, own
      files, own child agents). The entity is the admin of THIS
      domain.

    * Each child agent's CEIGAS domain — entity spawns bots via
      spawn_bot; each bot's bits can only be ≤ entity's bits.
      That's the recursion the filename encoding captured.

This module is the ENTITY-domain vocabulary. Platform-domain scopes
are deliberately ABSENT — if a future bug ever surfaces one on the
entity side, the lookup returns 0 and the gate denies. Defense in
depth.
"""
from __future__ import annotations

import logging
from typing import Iterable

import asyncpg

log = logging.getLogger("cseries.mastercode.meta_tool_caps")


# ============================================================================
# ENTITY-DOMAIN scope → capability bit
# ============================================================================
#
# Each scope in scopes.py that's legitimately part of the entity's own
# CEIGAS domain maps to one of the 8 capability bits per
# ceigas_invariants.INVARIANTS:
#
#   bit 0 (1):   read_context     — reading own state / configuration
#   bit 1 (2):   read_memory       — reading own entity_memory / sessions / signals
#   bit 2 (4):   write_context     — writing own state / configuration
#   bit 3 (8):   write_memory      — writing own entity_memory / sessions / queues
#   bit 4 (16):  execute_tool      — running own bots / code / builds in own pod
#   bit 5 (32):  external_api      — outbound web / github / MCP from this entity
#   bit 6 (64):  spawn_bot         — agent-spawning of own children (`dispatch`)
#   bit 7 (128): admin             — admin WITHIN the entity's OWN domain.
#                                    NOT platform admin — that's a different
#                                    CEIGAS domain owned by the operator.
#
# Platform-domain scopes are deliberately EXCLUDED. The following scopes
# from scopes.py do NOT appear here and would default-deny if anything
# ever required them at the entity level (lookup returns 0):
#   publish:apps, read/write:billing,
#   write:update_request, read/write:update_state,
#   read:backup_list, write:backup_restore, write:backup_trigger,
#   (read/write:backup_settings are NOT in that list — the user's own schedule
#   is entity-domain and IS mapped below)
#   execute/read:cseries_security, read:operator_audit.
# Those live in the operator's CEIGAS domain.

ENTITY_SCOPE_TO_BITS: dict[str, int] = {
    # READ_CONTEXT (1) — entity reads its own state
    "read:system":            1,  "read:system_status":   1,  "read:version":         1,
    "read:processes":         1,  "read:network":         1,  "read:files":           1,
    "read:workspace_files":   1,  "read:clipboard":       1,  
    "read:backup_settings":   1,  # the entity's own backup schedule
    # The host's own oversight ledger, kernel_op-exposed
    # (/api/oversight GETs): the finding rows with their evidence, and per machine
    # the posture, record holes, bastion token id, last-seen time and the
    # five-minute op's last error. All of it is this host's own data — its fold of
    # its own machines' beats and records, in its own pod — at the same bit
    # get_network reads the network with (kernel_ops.py says it in full).
    # write:oversight stays ABSENT on purpose: resolving a finding is the user
    # saying they dealt with it, and a 0-bit scope is how kernel_op makes that
    # structurally not the host's (the route is walled too).
    "read:oversight":         1,
    # Ships: the host's own ship runs, pipelines and machines,
    # kernel_op-exposed (/api/ships): its own records in its own pod.
    "read:ships":             1,
    # The host's own network map. GET /api/appearance/network and
    # /network/meta take read:topology so an app needs that scope to read the
    # map, and kernel_op (which exposes /api/appearance) reaches them only with
    # the scope here: absent, kernel_op denies them as platform domain and
    # check_kernel_ops fails. Topology is this host's own state: get_network
    # and nac_status read it at bit 1. Only these two routes are both exposed and
    # scoped read:topology; /api/network is not kernel_op-exposed.
    "read:topology":          1,
    # READ_CONTEXT, not READ_MEMORY: projects
    # are state — every project-read TOOL (list_projects, get_project,
    # get_project_activity) requires READ_CONTEXT, and write:projects already
    # sits at WRITE_CONTEXT below. At bit 2 this scope incidentally unlocked
    # the whole READ_MEMORY tool class for an entity holding only project
    # access; at bit 1 it yields exactly the bit the project tools require.
    "read:projects":          1,

    # READ_MEMORY (2) — entity reads its own memory and adjacent state
    "read:memory":            2,  "read:brain_memory":    2,
    "read:brain_personality": 2,  "read:brain_projects":  2,  "read:brain_sessions":  2,
    "read:brain_signals":     2,    "read:brain_state":     2,
    "read:brain_tasks":       2,  "read:brain_functions": 2,  "read:brain_corrections": 2,
    "read:session":           2,  "read:circles":         2,
    # The peer app lane's read half (kernel_op-exposed
    # /api/peer/app). Social state like circles, same bit.
    "read:peer_app":          2,
    # A group's roster, ballots and tallies (kernel_op-exposed
    # /api/groups). Social state like circles and the peer app lane, same bit.
    "read:group":             2,
    "read:address_book":      2,  "read:incidents":       2,  "read:tickets":         2,
    "read:policy":            2,  "read:portal_sources":  2,

    # WRITE_CONTEXT (4) — entity writes its own state / files
    "write:files":            4,  "write:clipboard":      4,  "write:network":        4,
    "write:input":            4,  "write:session":        4,  "write:projects":       4,
    # kernel_op: scopes that existed only on routes, now needed
    # in the entity vocabulary because the host can reach those routes.
    # publish:sites = the entity's OWN published sites (already modeled at
    # bit 4 by the manage_site/publish_site tools); write:appearance = the
    # entity's own desktop appearance. Platform-domain scopes (read:billing
    # etc.) stay ABSENT on purpose — kernel_op fail-closes on a 0-bit scope.
    "publish:sites":          4,  "write:appearance":     4,
    # The user's own backup schedule/target (/api/backup/settings,
    # kernel_op-exposed). Entity-domain like appearance. read:backup_list /
    # write:backup_restore / write:backup_trigger stay ABSENT (operator domain).
    "write:backup_settings":  4,
    # Ships: start one of the host's own ship runs (kernel_op
    # POST /api/ships/runs; the templated stop route is not host-reachable,
    # exposure being exact-path). A start only files the plan on
    # the person's approval device; nothing reaches a machine until they
    # approve it there, and each machine checks the plan itself. Clearing a
    # machine's pinned key is walled (kernel_ops): the person's act alone.
    "write:ships":            4,

    # WRITE_MEMORY (8) — entity writes its own memory / social / queue
    "write:memory":           8,  "write:brain_memory":   8,
    "write:brain_corrections": 8, "write:brain_tasks":    8,  "write:circles":        8,
    # The peer app lane's write half (grant a peer's copy of an
    # app, send to it). Same bit as write:circles — the host's own social
    # queue, and every send is bounded by the peer's grant.
    "write:peer_app":         8,
    # The group layer's write half — create a group, add/remove
    # members, set roles, open a ballot, cast this host's signed vote. Same bit
    # as write:circles: the host's own social state, and every mutation is
    # refused unless this host owns the group.
    "write:group":            8,
    "write:address_book":     8,  "write:domain_policy":  8,  "write:tickets":        8,
    "write:portal_sources":   8,

    # EXECUTE_TOOL (16) — entity executes its own tools / code in own sandbox
    "execute:code":          16,  "execute:benchmark":   16,  "execute:scan":        16,
    "execute:inference":     16,  "execute:install":     16,
    # Stopping a process in the entity's OWN pod. EXECUTE_TOOL and
    # not WRITE_CONTEXT — it acts on running code in the host's own sandbox,
    # exactly as system.restart_app (bit 16) does. The paired read scope
    # read:processes stays at bit 1, because seeing what runs is not this.
    "write:processes":       16,

    # EXTERNAL_API (32) — entity reaches external resources from its pod
    "execute:websearch":     32,
    # The fork-and-PR lane to a repo the host does not own. An
    # outbound call from this entity (same bit as the `gitea` tool itself);
    # nothing in the entity's own state changes.
    "write:gitea":           32,

    # SPAWN_BOT (64) — entity spawns its OWN child bots (entity domain →
    # child domain creation). `execute:spawn_bot` is an internal scope
    # (not in scopes.py — it's not an API-callable permission). It's
    # implicitly granted by `bots.execution=execute` access. Each child
    # bot's filename prefix still gets invariant-gated at bot_executor
    # time; SPAWN_BOT just authorizes the parent's act of dispatching.
    "execute:spawn_bot":     64,

    # ADMIN (128) — admin WITHIN the entity's own CEIGAS domain only.
    # No scope string maps here. Entity-domain admin primitives (rotating
    # own codebook, granting scopes to own child agents) would be MFA-gated
    # and are not built.
    # PLATFORM admin scopes (publish:apps / write:billing / etc.) live in
    # the operator's CEIGAS domain, not here.
}


# ============================================================================
# Meta-tool → required capability bits + domain
# ============================================================================
#
# Default-deny for any meta-tool name NOT in this dict. New meta-tools
# added in future ships MUST register here.

META_TOOL_CAPS: dict[str, dict] = {
    # ── Chat-search tools ────────────────────────────────────
    "local_grep":     {"required_bits": 1,           "domain": "core.entity",       "desc": "grep the host's codespace"},
    "research":       {"required_bits": 32,          "domain": "external.operator", "desc": "web research on the routed model (provider-native search; renamed from web_search 2026-05-27)"},
    "web_search":     {"required_bits": 32,          "domain": "external.operator", "desc": "model-aware everyday web search — native lane or the Orchestrator's research model (2026-07-11)"},
    "read_url":       {"required_bits": 32,          "domain": "external.operator", "desc": "read ONE named web page: curl_cffi fetch with a browser TLS fingerprint (aiohttp only as the degraded fallback) + trafilatura extraction, in-pod, no browser sidecar (2026-08-23)"},
    # `github` is the one public-GitHub name. It absorbed
    # github_repo's schema (whose executor it still calls for tree/read/import)
    # and superseded github_search, which carried a caps entry but NO schema —
    # so it was never offered to a model and never callable. Both old entries
    # are deleted rather than relabelled, so neither can return as a zombie.
    # `_exec_github_search` survives ONLY as an internal helper for
    # deep_research and the chat pre-fetch leg, which never pass this gate.
    "github":         {"required_bits": 32,          "domain": "external.operator", "desc": "public GitHub, one tool: repo search by criteria, code search across ~1M repos (grep.app, GitHub code search as fallback), repo activity, tree/file reads, and one explicit import into the user's own gitea — never pushes to GitHub (2026-08-23)"},
    # `gitea` reads the host's own Gitea forge — repos it owns
    # and repos the user granted it (Gitea app → Permissions). Five read ops
    # over the per-entity credential, plus op='propose' — a fork
    # under the host's own account and a PR upstream, never a push to the
    # upstream. Same bit as `github` (which likewise carries one write, its
    # import): an outbound call from this entity, nothing in its own state
    # changes.
    "gitea":          {"required_bits": 32,          "domain": "external.operator", "desc": "the host's own gitea: list repos it can see, one repo's facts, tree, one file, grep, and pull requests (list, one, files, reviews, CI, branches) — the consumer of the Gitea app's Permissions grant (2026-09-06) — plus op='propose', its write to a repo it does not own: fork under its own account, branch, one commit, pull request upstream, and op='comment' on a pull request; the upstream never receives a push and the host never merges"},
    "community_board": {"required_bits": 32,         "domain": "external.operator", "desc": "members community board read/post/watch (bridge community.* ops)"},

    # ── The plan→approve→build door ──
    "build":          {"required_bits": 16 | 4 | 32, "domain": "core.entity",       "desc": "the one build door: run (a build, or a compare across variants), compose/show a phased plan, build the approved phases on the pool (independent ones in parallel)"},

    # ── Project creation (bridges Circles → projects) ──
    # Requires WRITE_CONTEXT (4) to create the projects row and
    # WRITE_MEMORY (8) to backfill the arc of recent turns with the
    # new project_id. Both are entity-domain bits — no platform-side
    # gating needed.
    "create_project": {"required_bits": 4 | 8,       "domain": "core.entity",       "desc": "create a project (marker back to its conversation)"},
    # list_projects reads the projects table (+ a project_phases aggregate for
    # plan state) — the same data class as get_project / get_project_activity
    # (both READ_CONTEXT 1). At READ_MEMORY 2, a host granting read_context for
    # project work (as dispatch-agent.md teaches: projects ARE context) was
    # refused at the dispatch door. Projects are state, not memory; 1 is the
    # coherent classification.
    "list_projects":  {"required_bits": 1,           "domain": "core.entity",       "desc": "list/search the host's projects"},
    "project_phase":  {"required_bits": 4 | 8,       "domain": "core.entity",       "desc": "drive a project's phased workflow (phases/steps/status)"},
    "coding_bench":   {"required_bits": 2,           "domain": "core.entity",       "desc": "read the coding bench: engines (+ the default), protocols, routing, per-model track record"},
    # The taste lane: list/tokens_get read the user's stored
    # preferences; like/dislike/ban/unban/tokens_set write them. Same bits as
    # coding_protocol — entity-owned data the user dictates, no platform side.
    "preference":     {"required_bits": 1 | 4,       "domain": "core.entity",       "desc": "read/write the user's taste: like/dislike a model, harness or design choice, ban one from dispatch, store design tokens (per project or global)"},
    # Orchestrator → Multi-Agent Coding: list/get/status read the
    # protocol files + grant preflight; save/delete/record write them — the
    # user's instructions and the run outcomes the host records against them.
    "coding_protocol": {"required_bits": 1 | 4,      "domain": "core.entity",       "desc": "read/write the coding protocol files (Orchestrator → Multi-Agent Coding): list/get/status are reads; save/delete/record write the user's instructions + recorded outcomes"},
    # A project's build pipeline: status is a read; propose files a
    # decision ticket locked to the user — the setting changes only on their
    # 'apply' (tickets.decide_ticket_service), never through this tool.
    "pipeline":        {"required_bits": 1 | 4,      "domain": "core.entity",       "desc": "read a project's build pipeline (status) or propose a change the user approves on a ticket (propose); never writes the setting itself"},
    # The person's limits (2026-10-20): status is a read; propose files a card
    # locked to the person — the limit changes only on their 'apply'.
    "limits":          {"required_bits": 1,          "domain": "core.entity",       "desc": "read the person's limits (status) or ask them to change one on a card they decide (propose); never writes a limit itself"},
    "turn_type":       {"required_bits": 1,          "domain": "core.entity",       "desc": "list / show the host's turn types, or draft / ask to change / ask to retire one on a card the user decides; never activates or widens one itself"},
    # The Host's end of turn: writes the thread's
    # topic/title (WRITE_CONTEXT, the circles_threads row) and the user's
    # preferences + the thread goal as entity_memory rows (WRITE_MEMORY).
    # Same bits as create_project — entity-owned state, no platform side.
    "close_turn":      {"required_bits": 4 | 8,      "domain": "core.entity",       "desc": "end a turn with structured signal: turn type, useful/dead calls, faster hints, thread topic/title, the user's preferences, the thread goal, the handoff"},
    "archive_project": {"required_bits": 4 | 8,      "domain": "core.entity",       "desc": "archive (soft-delete) a project"},
    "project_adopt_thread": {"required_bits": 4 | 8, "domain": "core.entity",       "desc": "move a chat thread (or one episode) into a project — retag turns + derived memories, marker stays, nothing deleted"},

    # ── A compare's read side and the pick ──
    # A compare is started by `build` (action='run', variants=[…]) and carries
    # build's bits. compare only READS the session's candidates (no model call, no write), so it
    # needs the read bit; propose_commit writes the user's picks into the
    # project.
    "review.compare":        {"required_bits": 1,           "domain": "core.entity", "desc": "read a finished compare's candidates side by side (no pick)"},
    "review.propose_commit": {"required_bits": 4,           "domain": "core.entity", "desc": "apply the user's per-file picks from a compare into the project"},
    # adopt is the plain verb propose_commit is not: it copies a run's files
    # into the project unchanged. Same write bit as propose_commit — it is
    # the same act (project files change) with no model in the loop.
    "review.adopt":          {"required_bits": 4,           "domain": "core.entity", "desc": "use a run's files as-is in its project (reversible)"},
    "review.adopt_undo":     {"required_bits": 4,           "domain": "core.entity", "desc": "undo an adopt: restore what it overwrote, remove what it created"},

    # ── system.* app control ──
    # A tool in the dispatch set and offered to hosts but missing here is
    # default-denied on every call (visible in the catalog, dead on call).
    # (Single-harness runs go through `build(harness=...)`.)
    # system.* is operational control over the host's
    # OWN pod apps — same tier as install (execute_tool); app_status is read-only.
    "system.restart_app":    {"required_bits": 16,          "domain": "core.entity", "desc": "restart one of the host's own pod apps"},
    # start/stop (resident lifecycle): the switch for an always-on
    # process app, deterministic and person-free, so schedule_task's op lane
    # can run "start at 09:00 / stop at 17:00" with no model in the loop.
    "system.start_app":      {"required_bits": 16,          "domain": "core.entity", "desc": "start one of the host's own process apps with no window open (a resident daemon stays up)"},
    # operation "stop": it only reduces running activity (stops the app and
    # keeps it off), so it is the one meta-tool the kill switch lets through,
    # (runtime/tools.py _authorize_meta_tool, asking
    # ceigas_kill_switch.kill_switch_lets_through).
    "system.stop_app":       {"required_bits": 16,          "domain": "core.entity", "operation": "stop", "desc": "stop one of the host's own process apps, resident or not"},
    "system.app_status":     {"required_bits": 1,           "domain": "core.entity", "desc": "read the status of the host's own pod apps"},
    # app_probe: ask the running app itself — one HTTP
    # request on a PATH, its log with a filter, or a read of its own
    # appdata. A read bit, and jailed to apps this host owns: it is
    # strictly less invasive than restart_app (bit 16), which the host
    # already has.
    # The UNION is 1|16 because op='request' is a network call, and what a GET
    # does is the app author's choice — the same bit start/stop/restart carry.
    # files/logs are declared read-safe at 1 below (READ_SAFE_OPS), so reading
    # an app's log or config still costs only a read.
    # The reach is pinned to the app's own origin (verified against
    # scheme-relative, userinfo, CRLF and percent-encoded traversal, with
    # redirects off), so this is about the app's OWN side effects, not escape.
    "app_probe":             {"required_bits": 1 | 16,      "domain": "core.entity", "desc": "ask one of the host's own running apps: an HTTP path (16), or its log/data files (1)"},
    # A browser agent the USER recorded: op='run' drives a real
    # browser against the agent's declared site (16); list/get/runs are reads
    # of the host's own agent files (1, READ_SAFE_OPS). The credentials it
    # uses are typed by the vault broker under its own gate, never by this.
    "browser_agent":         {"required_bits": 1 | 16,      "domain": "core.entity", "desc": "run / resume / list a browser agent the user recorded: a real browser on the agent's declared site (16); list/get/runs (1)"},

    # ── Environment knowledge (host-environment awareness) ──
    # Read-only: the host reads its own installed apps + their catalog facts so
    # it can explain its environment to the user. READ_CONTEXT (1) — own state.
    "explore_environment":   {"required_bits": 1,           "domain": "core.entity", "desc": "list installed apps for environment awareness"},
    "describe_app":          {"required_bits": 1,           "domain": "core.entity", "desc": "describe one installed app from its catalog facts"},
    "diagnose":              {"required_bits": 1,           "domain": "core.entity", "desc": "the host's own machine faults and recent failures (a provider's refusal, a tool's error) as data, with the exact configuration call where one fixes it; deterministic, no model (2026-09-06; failures 2026-10-12)"},
    "check_page":            {"required_bits": 1,           "domain": "core.entity", "desc": "load one of the host's own app pages in the browser sidecar and report what a browser sees (2026-09-05)"},
    # Capabilities surface: read the generated platform-capability
    # doc (in-pod file read, no external reach); escalate a verified gap into
    # the community pipeline (board thread + governed proposals PR — same
    # external reach class as community_board).
    "capabilities":          {"required_bits": 1,           "domain": "core.entity", "desc": "search the generated 'can Privatae Host do X?' capability cards + doc (version-exact; a miss is a word miss, not proof of absence)"},
    "propose_capability":    {"required_bits": 32,          "domain": "external.operator", "desc": "file a capability gap as an Ideas thread + governed community/proposals PR (user-consented)"},
    # The bug cache: every op is a bridge round-trip to the
    # operator, which refuses any Host that is not a platform operator. One
    # bit (external_api) is what a dispatched validator agent is granted; the
    # host-vs-agent split (who may read a pending report, who may record a
    # verdict, who may mark a fix) is enforced by the tool on the grant's
    # presence, not by bits — see runtime/bug_review.py.
    "bug_review":            {"required_bits": 32,          "domain": "external.operator", "desc": "the platform's bug cache for an operator Host: list/read validated reports, dispatch a validator per pending one, record a verdict (agent) or a fix (host)"},
    # The support queue's collect: one bridge read per pass; the
    # operator refuses any Host that is not a platform operator. The verdicts
    # are not a tool — the platform applies the user's decide on the card
    # (server/routes/tickets.py) — so no bit here can send one.
    "support_collect":       {"required_bits": 32,          "domain": "external.operator", "desc": "the platform's support queue for an operator Host: pull the tickets other Hosts sent the platform into this Host's own decisions (Circles: Waiting on you), one card each"},
    # Caps rows for seven tools with schemas + dispatch + working handlers:
    # without a row _authorize_meta_tool default-denies every call — the
    # dead-tool class the schema⊆dispatch⊆caps gate exists to end. Bits/domain
    # modeled on their nearest siblings (coding_bench for the bench reads,
    # keys_manage for the wiring writer, build for ladder.run).
    "harness_costs":         {"required_bits": 2,           "domain": "core.entity", "desc": "read per-harness/model cost truth from the host's own run ledger"},
    "harness_usage_windows": {"required_bits": 2,           "domain": "core.entity", "desc": "harness token consumption over the subscription refresh windows (session/5h/24h/7d)"},
    "harness_transcript":    {"required_bits": 2,           "domain": "core.entity", "desc": "read one coding run's transcript from the host's own bench history"},
    "credential_wiring":     {"required_bits": 1 | 4,       "domain": "core.entity", "desc": "read/set which Keys entry each application/lane uses (wiring only — never credential bytes)"},
    "ladder.run":            {"required_bits": 16 | 4 | 32, "domain": "core.entity", "desc": "start an escalation-ladder climb (build attempts on the pool)"},
    # Same bits as its lane sibling mesh_harness_run (16 | 32, below): lab_run
    # dispatches a coding harness (mode=lab) onto the pool through the same
    # run_harness_op and writes nothing to entity context, so no write bit.
    # code_exec_guard lists it as code-exec and tool_access refuses it in any
    # dispatch scope. Without this row it is default-denied at call and fails
    # three smoke gates (tool lanes, host build door, capability coverage).
    "lab_run":               {"required_bits": 16 | 32,     "domain": "core.entity", "desc": "run a LAB test: sealed lab of N hosts on the lab server, the app under test in each, a harness-driven runbook inside (lab rate, gate='auto')"},
    "ladder.status":         {"required_bits": 2,           "domain": "core.entity", "desc": "read a ladder run's rungs/attempts/verdicts"},
    "ladder.decision":       {"required_bits": 8,           "domain": "core.entity", "desc": "record the human gate's accept/reject on an awaiting-review ladder run"},
    "dcn":                   {"required_bits": 1 | 32,      "domain": "core.entity", "desc": "the user's Distributed Context Network: list/status + governed range-reads over stores THEY own; store grants are user-approved drafts"},
    # open_app is a UI navigation aid: the call is relayed to the dashboard
    # shell, which opens the window under the USER's own session (the app's own
    # token is minted client-side). READ_CONTEXT (1) — it touches no entity or
    # platform state.
    "open_app":              {"required_bits": 1,           "domain": "core.entity", "desc": "open an installed app window for the user"},

    # ── Network action layer — host reads its own network (READ_CONTEXT 1)
    # and PROPOSES drafts (WRITE_CONTEXT 4 == write:network). Drafts are inert;
    # the user approves (a widen needs their step-up). The host can never
    # approve its own proposal — approve/sign stays a user-only route.
    "get_network":           {"required_bits": 1,           "domain": "core.entity", "desc": "read the host's own network (bastions/peers/nac/egress/grants)"},
    # machine_map: op='read' is a table read (1,
    # READ_SAFE_OPS). op='refresh' reaches the management relay on the user's
    # machine through nac_mgmt.run_mgmt_op — the same relay mgmt_run drives —
    # so it carries the same coarse bit (16) even though the two ops it runs
    # are read-only on the box; the user's signed grant (deny beats allow) is
    # the wall. With bits=1 alone a read-only scoped
    # agent could dispatch a scan of the user's desktop.
    "machine_map":           {"required_bits": 1 | 16,      "domain": "core.entity", "desc": "the machine map: read the kept apps/windows (1); refresh / tree ask the box through the grant-gated management lane (16)"},
    "propose_egress_rule":   {"required_bits": 4,           "domain": "core.entity", "desc": "draft an egress rule (user approves; allow needs step-up)"},
    "propose_mgmt_grant":    {"required_bits": 4,           "domain": "core.entity", "desc": "draft a management grant (user approves with step-up)"},
    "propose_nac_policy":    {"required_bits": 4,           "domain": "core.entity", "desc": "draft a NAC policy rule (user approves; allow needs step-up)"},
    # MCP connections: servers / tools read the Host's own connections (1, READ_SAFE_OPS);
    # connect drafts a row and an egress allow rule the person approves with
    # their code (4), disconnect revokes both. op='tools' may fetch the list
    # through the gate, so the union carries external_api (32).
    "mcp_connections":       {"required_bits": 1 | 4 | 32,  "domain": "core.entity", "desc": "the MCP servers the host connects to: list them and their tools (read), draft a connection the person approves in the Network app, disconnect one"},
    # The Host's own database:
    # sql reads its records (1|2) and writes its own schema (4), as host_world,
    # which writes nothing outside `world`. No external_api: nothing leaves the pod.
    "data":                  {"required_bits": 1 | 2 | 4,   "domain": "core.entity", "desc": "the host's own database: SQL in its schema `world` and over its own records, describe, snapshot history and restore"},

    # ── Network/bastion GOVERNANCE commit-side + bastion EXECUTION ──
    # Commit companions to get_network/propose_* (read + draft). A per-host COMMIT of a network
    # change needs write:network (bit 4); reading state needs read_context (1). The real widen-gate
    # is the user's step-up (TOTP) enforced in the route/service, not a bit. decision_resolve writes
    # the host's own governance queue (8 / core.governance, like escalate). Bastion EXECUTION is
    # every Host's (mesh_harness_run: mode='service' only, but for the operator):
    # execute_tool (16), +external_api (32) for the agent that reaches providers — the user's
    # signed grant (verified in-route) is the authoritative wall, the bit is the coarse gate.
    "nac_status":            {"required_bits": 1,           "domain": "core.entity",     "desc": "read the host's full NAC + bastion governance state"},
    "approve_nac_draft":     {"required_bits": 4,           "domain": "core.entity",     "desc": "commit a pending NAC draft (widen needs user step-up)"},
    "revoke_nac_rule":       {"required_bits": 4,           "domain": "core.entity",     "desc": "revoke an active NAC policy/egress/grant"},
    "quarantine_bastion":    {"required_bits": 4,           "domain": "core.entity",     "desc": "cut/restore a bastion's whole-mesh reach"},
    "suspend_bastion":       {"required_bits": 4,           "domain": "core.entity",     "desc": "pause/resume a bastion's mgmt+harness lanes"},
    "manage_bastion":        {"required_bits": 4,           "domain": "core.entity",     "desc": "bastion lifecycle: enroll/revoke/update/list"},
    # Pairing grants: a per-host COMMIT on the user's own anchored machine.
    # The widen wall for approve is the user's step-up inside the service, not a bit.
    "manage_pairing":        {"required_bits": 4,           "domain": "core.entity",     "desc": "approve/deny/revoke a pairing on an anchored machine, or consent to an anchor rotation (widens need user step-up)"},
    "decision_resolve":      {"required_bits": 8,           "domain": "core.governance", "desc": "resolve one of the host's own pending decisions"},
    "mgmt_run":              {"required_bits": 16,          "domain": "core.entity",     "desc": "operate a granted server in the host's mesh (signed grant gates)"},
    "mesh_harness_run":      {"required_bits": 16 | 32,     "domain": "core.entity",     "desc": "keep a server running on a granted mesh server; agent runs are the operator's (signed grant gates)"},
    # Root commands: execute_tool + write_context, the bits a
    # write to a machine already takes. The walls that matter are elsewhere:
    # the machine's own standing grant, the turn's taint state, and no agent.
    "machine_exec":          {"required_bits": 16 | 4,      "domain": "core.entity",     "desc": "run one command as root on a machine whose bastion granted this host root commands (refused in a turn that read untrusted content)"},

    # ── Operator-Host private meta-tools ─────────────────────────────
    "state_store":    {"required_bits": 4,           "domain": "core.memory",       "desc": "write host context KV"},
    "state_load":     {"required_bits": 1,           "domain": "core.memory",       "desc": "read host context KV"},
    "escalate":       {"required_bits": 8,           "domain": "core.governance",   "desc": "file a ticket / decision (writes to own queue)"},

    # ── Bridge-op tools (entity → platform-domain crossing) ────────
    # These tools cross OUT of the entity's CEIGAS domain into the
    # operator's platform domain. The entity-side gate confirms the entity has the
    # bits within its OWN domain to reach the bridge; the bridge itself
    # re-validates platform-side. Defense in depth.
    "code_read":            {"required_bits": 1,       "domain": "infra.codebase",  "desc": "read repo file"},
    "code_grep":            {"required_bits": 1,       "domain": "infra.codebase",  "desc": "grep repo"},
    "code_tree":            {"required_bits": 1,       "domain": "infra.codebase",  "desc": "list repo tree"},
    # The platform proposal tool: retired
    # (tool_lanes.RETIRED_PROPOSE_TOOLS); a platform change is gitea op='propose'.
    # docker_build / docker_push / git_tag / k8s_set_image / k8s_set_env /
    # systemctl_run: retired (tool_lanes.RETIRED_SHIP_TOOLS).
    "k8s_rollout_status":   {"required_bits": 1,       "domain": "infra.codebase",  "desc": "k8s rollout status"},
    "inbox_send":           {"required_bits": 8,       "domain": "core.memory",     "desc": "inbox message send"},
    "calendar_log":         {"required_bits": 8,       "domain": "core.memory",     "desc": "calendar event log"},
"install_app":        {"required_bits": 16, "domain": "core.entity", "desc": "install an app from the store onto this host"},
    "uninstall_app":      {"required_bits": 16, "domain": "core.entity", "desc": "uninstall a non-native app from this host"},
    "refresh_app_store":  {"required_bits": 16, "domain": "core.entity", "desc": "on-demand store-upgrade sweep of installed apps"},
    "list_app_catalog":   {"required_bits": 1,  "domain": "core.entity", "desc": "list store apps joined with this host's install state"},
    "purchase_app":       {"required_bits": 16, "domain": "core.entity", "desc": "records a free entitlement; paid apps go through Stripe checkout"},
    "manage_site":        {"required_bits": 4,  "domain": "core.entity", "desc": "manage the host's own published sites (list/analytics/unpublish/suspend/resume/restart)"},
    "manage_site_domain": {"required_bits": 4,  "domain": "core.entity", "desc": "manage custom domains + DNS records on the host's own sites"},
"inbox_read":           {"required_bits": 2,       "domain": "core.memory",     "desc": "read one inbox message (marks read)"},
"circles_manage": {"required_bits": 2 | 8, "domain": "core.entity", "desc": "read + manage the host's Circles comms (conversations, threads, group circles)"},
"calendar_read": {"required_bits": 1, "domain": "core.entity", "desc": "read one of the host's own calendar/timeline events by id"},
"phone_alias": {"required_bits": 2 | 8, "domain": "core.entity", "desc": "read/set/remove own phone alias + identity-only number lookup"},
"memory_search": {"required_bits": 2, "domain": "core.memory", "desc": "search the host's own entity_memory by content"},
"chat_threads": {"required_bits": 2, "domain": "core.memory", "desc": "browse/reopen the host's own conversation log (threads, episodes, turns) — read-only"},
    "memory_curate": {"required_bits": 8, "domain": "core.memory", "desc": "add a preference or relationship note, or replace or drop one by id"},
    "memory_scope": {"required_bits": 8, "domain": "core.memory", "desc": "set where one preference may ride: global, or one project / app / thread"},
"embedding_route": {"required_bits": 1 | 8, "domain": "core.memory", "desc": "inspect the host's embedding config, or route embeds to the user's own machine (use_bastion); no platform embedder exists"},
"embed_jobs":     {"required_bits": 8,           "domain": "core.memory",       "desc": "queue a durable background embedding job over own memory banks"},
"training_memory":  {"required_bits": 2, "domain": "core.memory", "desc": "read the host's learned behavioral_corrections (Training Memory)"},
"review_session": {"required_bits": 1, "domain": "core.entity", "desc": "read one compare session (manifest, per-variant state, diffs)"},
"workspace_file": {"required_bits": 1 | 4, "domain": "core.entity", "desc": "host workspace file I/O (read/list + write/delete) in its own sandboxed workspace"},
"search_my_code": {"required_bits": 1, "domain": "core.entity", "desc": "hybrid search all the code the host can read: its projects and its readable gitea repos"},
    "index_my_code":  {"required_bits": 16 | 4, "domain": "core.entity", "desc": "incrementally (re)index one of the host's own named projects"},
"harness_connections": {"required_bits": 1, "domain": "core.entity", "desc": "read-only harness connection state (installed/connected/valid per coding harness)"},
# status/recommend READ (bit 1); set WRITES the routing table (bit 4) — the host
# may only change what answers when it holds the write bit, same as any config.
"model_routing": {"required_bits": 1 | 4, "domain": "core.entity", "desc": "read the routing table, recommend routes for unrouted roles, and set a role's provider+model"},
"model_bench": {"required_bits": 1 | 4 | 32, "domain": "core.entity", "desc": "the text-role admission bench: status/suite read the run ledger (READ_CONTEXT 1); run drives the fixed matrix against a candidate on the user's key (EXTERNAL_API 32 — every call leaves the pod) and records the run (WRITE_CONTEXT 4)"},
    # 8 / core.governance: "the host records something with the platform".
    # Free-text platform help is the `tickets` tool's support ops (same bits);
    # a crash report is a fixed metadata schema with no free text, open to
    # every host behind a per-entity rate limit at the bridge.
    "report_crash": {"required_bits": 8, "domain": "core.governance", "desc": "report a platform-component crash to the operator (metadata only; no dump, no free text)"},
"integrations_manage": {"required_bits": 4, "domain": "core.entity", "desc": "manage data-source integrations: list providers/connections, inspect, revoke (write:portal_sources)"},
"list_skills": {"required_bits": 1, "domain": "core.entity", "desc": "list the host's skill .md files (read-only inventory)"},
"publish_app":      {"required_bits": 4,      "domain": "core.entity", "desc": "manage the host's own published store apps (my_apps/sales/set_price/yank) via the caller-scoped bridge"},
"ship_project_app": {"required_bits": 16 | 4, "domain": "core.entity", "desc": "pack+sign+install a completed project as a provenance=custom app on this host"},
"publish_site":     {"required_bits": 4,      "domain": "core.entity", "desc": "publish a Workbench project's built web output as a live site on the public webapp zone (caller-scoped bridge; a new site goes live public)"},
"tickets": {"required_bits": 1 | 8, "domain": "core.governance", "desc": "the Tickets queue: decisions for the user (list/get/badges read; decide/dismiss write) + platform-support tickets (support_list/support_get read; support_open drafts locally, support_update rewrites a draft locally, support_reply/support_close write — the USER approves what is shared before anything leaves the pod)"},
"host_message": {"required_bits": 2 | 8, "domain": "core.entity", "desc": "host-to-host DM: send + list conversations + read thread + open (read+write inbox_messages, like circles_manage)"},
"contacts_manage": {"required_bits": 2 | 8, "domain": "core.entity", "desc": "address book + connection requests: list/add/update/remove contacts, accept/deny inbound requests, request-by-phone"},
"personality_state": {"required_bits": 2,     "domain": "core.memory",  "desc": "read the host's own 10D personality settings (user-set weights, envelope, mode; never self-adjusting)"},
# The user's explanation level (how much the host explains). get
# reads it (2); set writes ONE key of behavioral_params (8) and only when the
# user asked in words — the same service Settings → Response Shaping uses.
"explanation_level": {"required_bits": 2 | 8, "domain": "core.memory",  "desc": "the user's explanation level (full / new / terms): read it, or set it when the user asks in words — never inferred"},
# Reads the host's own rules / harness / memory / setup tables (1|2) and
# writes ONE inbox row (8). The weekly standing op every host is seeded with.
"host_changelog":    {"required_bits": 1 | 2 | 8, "domain": "core.memory", "desc": "the weekly 'state of the host' changelog: what changed since last time + what is not set up yet, delivered as a notice on the Support card in Circles"},
# The oversight ledger. READS the host's own network — the same bit 1
# get_network needs (:331) — and WRITES rows into the host's own ledger plus, at
# most once a pass, one inbox row (8). Never bit 4: it changes nothing on any
# machine and uses no grant. Folding what a box already reported is not an
# operation ON that box.
"oversight_ingest":   {"required_bits": 1 | 8, "domain": "core.entity",  "desc": "fold what the machines reported (alerts/detection/posture/offline) into the host's oversight ledger"},
"entity_tasks":       {"required_bits": 2 | 8, "domain": "core.entity",  "desc": "view + cancel/delete/purge the host's own task queue"},
"context_ingest": {"required_bits": 2 | 8, "domain": "core.memory", "desc": "stage external material into the host's context-intake layer, search it, and promote staged entries into its own memory"},
"context_backend": {"required_bits": 1 | 4, "domain": "core.entity", "desc": "see/list/set WHERE the host stores its context content (pg/s3/bastion) + the host-local index of which file lives on which machine/bucket"},
"set_project_manifest": {"required_bits": 4, "domain": "core.entity", "desc": "write+validate a project's app.yaml (host-scoped; rolled back if invalid)"},
"get_project": {"required_bits": 1, "domain": "core.entity", "desc": "read one of the host's own projects (phases/steps/routing)"},
"update_project": {"required_bits": 4, "domain": "core.entity", "desc": "rename/re-status/archive one of the host's own projects"},
"get_project_activity": {"required_bits": 1, "domain": "core.entity", "desc": "read a project's live activity feed (tool calls + research findings)"},
"set_project_routing": {"required_bits": 4, "domain": "core.entity", "desc": "set/clear the preferred model per domain on a project"},
    # Autofill: brokers a USE of the user's saved form entries,
    # exactly like keys_manage brokers a use of a key — same bits, same
    # promise. It never returns a stored value, and filling is refused
    # without the user's standing per-site grant.
    "autofill":              {"required_bits": 1 | 4 | 16, "domain": "core.entity", "desc": "fill a site's form from the entries the user saved for it, under a standing per-site grant — never returns a saved value"},
"keys_manage": {"required_bits": 1 | 4 | 16, "domain": "core.entity", "desc": "manage the host's own sealed credential vault: list metadata / add / revoke / broker a USE — NEVER returns raw bytes (reveal is TOTP-gated, Keys-app UI only)"},
"connect_compute": {"required_bits": 1 | 4 | 16 | 32, "domain": "core.entity", "desc": "register/manage the host's OWN BYO inference compute (local endpoint or RunPod batch); tokens sealed in the vault, dispatch via the broker; BYO confined to non-prompt roles (leak-guarded)"},
"schedule_task": {"required_bits": 2 | 8, "domain": "core.entity", "desc": "create/list/cancel the host's own scheduled (one-time or recurring) offline tasks — calendar events the scheduled-task runner fires + re-arms"},
"task_template": {"required_bits": 2 | 8, "domain": "core.entity", "desc": "create/list/delete the host's own reusable scheduled-task templates (runbooks) — name + prompt and/or steps + optional default recurrence"},
"reachout": {"required_bits": 1 | 8 | 32, "domain": "core.memory", "desc": "how the host reaches its person: list its channels, subscribe one to an inbox event, fire a test, remove what it created \u2014 READ_CONTEXT (1), WRITE_MEMORY (8) because a fire is a message to the user, EXTERNAL_API (32) because firing one leaves the pod"},
"reachout_channel": {"required_bits": 1 | 8 | 32, "domain": "core.memory", "desc": "BUILD a reach-out channel \u2014 binds one of the host's sealed credentials to an outbound destination, so it carries EXTERNAL_API (32) like every other outbound tool, runs in the host's own turn but never on a schedule, and demands the user's step-up code for an http_call action"},
"model_route": {"required_bits": 1 | 4, "domain": "core.entity", "desc": "inspect/switch the host's per-role model routing (OpenRouter+local catalog, current+effective, switch) — BYO leak-guard refuses self-for-prompt-roles"},
"inference_catalog": {"required_bits": 1, "domain": "core.entity", "desc": "list the available inference model catalog (OpenRouter + local) the model-picker shows — read-only"},
"image_generate": {"required_bits": 1 | 32, "domain": "core.entity", "desc": "generate pictures on the user's own image route / look at pictures with the Host's own model / list the gallery"},
# The host's OWN context-file store (entity_context) — installed assets, staged
# state. Ported inline from public_bots/00000111_context.py when the bot lane was
# retired; these bits ARE that filename's `00000111` prefix, so the
# authorization the filename used to encode is now encoded here. runtime/
# context_ops.py rebuilds the same SandboxedPool(bits) the bot ran behind.
"context": {"required_bits": 1 | 2 | 4, "domain": "core.entity", "desc": "the host's own context-file store (entity_context): read/list/search/status reads, write/delete actions — backend-aware (pg / the host's s3 / its bastion)"},
# Same mask as `context`, and for the same reason: this is the host's own
# memory, read and written in its own pod DB. runtime/link_ops.py rebuilds the
# identical SandboxedPool(bits) wall, so the number lives in two places on
# purpose — keep them in step.
"links": {"required_bits": 1 | 2 | 4, "domain": "core.entity", "desc": "the host's saved links (entity_links): save/list/search/get reads and writes, remove by id — the link memory every plan gets, which the linkding catalog app syncs against rather than replaces"},
"task_record": {"required_bits": 1 | 2 | 4, "domain": "core.entity", "desc": "a task's shared record (goal_checks, task_coverage, task_sources, task_findings): checks, coverage and quote-checked findings, read and written per project through runtime/task_record.py"},
"import_repo": {"required_bits": 16 | 4, "domain": "core.entity", "desc": "import an existing codebase from a Git URL (GitHub pull) into a new/existing project workspace — shallow-clones the repo so the host can index/build on it"},
"artifact": {"required_bits": 1 | 4 | 16, "domain": "core.entity", "desc": "create/build/render/list/convert/delete the host's own disposable live ARTIFACTS — distinct from apps (project-stored, NOT app-sdk-bound until the user converts)"},
# The generic kernel-route surface (runtime/kernel_ops.py). The
# declared floor here is read_context; the REAL wall is per-call inside
# run_kernel_op: exposure allowlist, walled paths, and the target route's own
# scope translated through ENTITY_SCOPE_TO_BITS (0-bit scopes fail closed).
"kernel_op": {"required_bits": 1, "domain": "core.entity", "desc": "call one EXPOSED kernel /api route via a minted single-scope token — per-call scope bits + exposure allowlist enforced inside"},
}


# ============================================================================
# Per-(domain, access_level) → implicit scope set
# ============================================================================
#
# Domain-aware mapping per Invariant 3 (Brain-Mapped Domain Isolation): each
# domain has its own meaningful access levels. The entity's effective
# scope set is the UNION over its entity_domain_access rows. Domains
# the entity has no row for contribute nothing.

DOMAIN_ACCESS_TO_SCOPES: dict[tuple[str, str], set[str]] = {
    # core.entity — own state, sessions, projects, tasks
    ("core.entity", "read"): {
        "read:system", "read:system_status", "read:version",
        "read:brain_state",
    },
    ("core.entity", "read_write"): {
        "read:system", "read:system_status", "read:version",
        "read:brain_state",
        "read:session", "write:session",
        "read:projects", "write:projects",
        "read:brain_tasks", "write:brain_tasks",
    },
    ("core.entity", "execute"): {
        "read:system", "read:system_status", "read:version",
        "read:brain_state",
        "read:session", "write:session",
        "read:projects", "write:projects",
        "read:brain_tasks", "write:brain_tasks",
        "execute:code", "execute:inference",
    },
    ("core.entity", "admin"): {
        "read:system", "read:system_status", "read:version",
        "read:brain_state",
        "read:session", "write:session",
        "read:projects", "write:projects",
        "read:brain_tasks", "write:brain_tasks",
        "execute:code", "execute:inference",
        "read:brain_personality",
    },

    # core.memory — own memory and corrections
    ("core.memory", "read"): {
        "read:memory", "read:brain_memory", "read:brain_corrections",
    },
    ("core.memory", "read_write"): {
        "read:memory", "write:memory",
        "read:brain_memory", "write:brain_memory",
        "read:brain_corrections", "write:brain_corrections",
    },
    ("core.memory", "admin"): {
        "read:memory", "write:memory",
        "read:brain_memory", "write:brain_memory",
        "read:brain_corrections", "write:brain_corrections",
        "read:brain_signals", "read:brain_functions",
        "write:tickets", "read:tickets",  # ticket queue lives in memory domain
    },

    # core.cognitive — inference + tool execution
    ("core.cognitive", "read"): set(),
    ("core.cognitive", "execute"): {
        "execute:inference", "execute:code",
        "execute:scan", "execute:benchmark",
    },

    # core.governance — read-only for entity (only the operator writes here)
    ("core.governance", "read"): {"read:policy", "read:incidents"},

    # bots.execution — entity spawns / runs its OWN child bots
    ("bots.execution", "read"): set(),
    ("bots.execution", "execute"): {
        "execute:code", "execute:benchmark", "execute:scan",
        "execute:spawn_bot",  # implicit — see ENTITY_SCOPE_TO_BITS note
    },

    # infra.codebase — codebase access. Admin grant is special (operator-Host class).
    ("infra.codebase", "read"): {"read:files", "read:workspace_files"},
    ("infra.codebase", "read_write"): {
        "read:files", "write:files", "read:workspace_files",
    },
    ("infra.codebase", "admin"): {
        "read:files", "write:files", "read:workspace_files",
        "execute:code", "execute:install",
    },

    # container.relay — own pod's network, clipboard, processes
    ("container.relay", "read"): {
        "read:network", "read:clipboard", "read:processes",
    },
    ("container.relay", "read_write"): {
        "read:network", "write:network",
        "read:clipboard", "write:clipboard",
        "read:processes", "write:input",
    },

    # external.operator — outbound reach (web search, github, portals)
    ("external.operator", "read"): {"execute:websearch"},
    ("external.operator", "read_write"): {
        "execute:websearch",
        "read:portal_sources", "write:portal_sources",
        # The fork-and-PR lane. Bit 32 alone is already granted at
        # read level (execute:websearch), so the gitea executor checks for
        # THIS scope on op='propose' — a read-level external.operator grant
        # can read a repo but cannot propose to it.
        "write:gitea",
    },
}


# ============================================================================
# PER-OP capability bits
# ============================================================================
#
# `required_bits` above is declared PER TOOL, as the union of everything the
# tool's ops can do. That union is what an agent must hold to call the tool at
# all — so `context_ingest`, whose ops span search (a read) and promote (a
# write), demands WRITE_MEMORY even from an agent that only ever wanted to
# search. Eight tools force that over-grant, and an over-grant is the signal
# that a task wasn't decomposed: the agent doing the reading and the agent
# doing the writing should not be the same agent.
#
# This map declares the READ-SAFE ops only. An op listed here is authorized
# against ITS bits; an op that is absent, unrecognized, or ambiguous falls back
# to the tool's union. That direction is deliberate — this table can only ever
# TIGHTEN authorization, never widen it, so a missing entry is a missed
# optimization and never a hole.
#
# Ops were read from each tool's live schema enum, not from memory.

# NOT DECLARED HERE: `context`. Its reads (read/list/search/status) look exactly
# like every other entry, and adding them is a one-liner — but it would WIDEN
# agent authority, not preserve it. As a bot, `context` had no per-op bits at
# all: required_bits_for() resolved its filename mask (7), so an agent granted
# only read_context was DENIED context(action='read'). Declaring the read ops
# here would let that agent through. That is a real decision about what a
# read-only agent may see (the host's own context files — installed assets,
# staged state), so it is the operator's to make, and it deliberately did not ride
# along with the bot-lane retirement. The host's OWN direct reads are unaffected:
# those come from tool_access.read_safe_ops, which derives them from the
# schema's action enum and never consulted this table.
READ_SAFE_OPS: dict[str, dict[str, int]] = {
    # ── read_context (1) reads on the admission bench (run is the spend) ──
    "model_bench":     {"status": 1, "suite": 1},
    # ── read_memory (2) reads on read_memory|write_memory tools ──────────
    "circles_manage":  {"list_conversations": 2, "read_thread": 2, "list_circles": 2},
    "contacts_manage": {"list": 2, "card": 2},
    "context_ingest":  {"search": 2, "list": 2, "files": 2, "read": 2},
    "entity_tasks":    {"list": 2},
    "host_message":    {"conversations": 2, "thread": 2},
    "phone_alias":     {"get": 2, "lookup": 2},
    "schedule_task":   {"list": 2},
    "task_template":   {"list": 2},

    # ── read_context (1) reads on read_context|write_* tools ─────────────
    # Reading which channels exist and what has fired is a pure read of the
    # host's own configuration — READ_CONTEXT (1). Only that op: everything
    # else on this tool builds or fires something.
    "reachout":             {"list": 1},
    "tickets":              {"list": 1, "get": 1, "badges": 1, "support_list": 1, "support_get": 1},
    "embedding_route":      {"current": 1, "dims": 1, "bastions": 1},  # catalog retired
    "workspace_file":       {"list": 1, "read": 1},
    # Reading an app's own log or data files is a read; asking the app over
    # HTTP is not, so 'request' is absent here and falls back to the union.
    "app_probe":            {"files": 1, "logs": 1},
    "browser_agent":        {"list": 1, "get": 1, "runs": 1},
    "machine_map":          {"read": 1},
    "model_route":          {"current": 1, "catalog": 1},
    "context_backend":      {"get": 1, "list": 1, "bastions": 1, "cabinets": 1},
    "credential_wiring":    {"get": 1},
    "autofill":             {"saved": 1, "list": 1},
    "coding_protocol":      {"list": 1, "get": 1, "status": 1},
    "pipeline":             {"status": 1},
    "turn_type":            {"list": 1, "show": 1},
    "limits":               {"status": 1},
    "preference":           {"list": 1, "tokens_get": 1},
    "artifact":             {"list": 1, "history": 1},
    "keys_manage":          {"list": 1},
    "dcn":                  {"stores": 1, "status": 1, "list": 1, "stat": 1},
    "connect_compute":      {"list": 1},
    # The Host's own MCP connections and the screened tool lists it stored
    # (op='tools' may fetch a list again through the gate; it changes nothing
    # the person approved).
    "mcp_connections":      {"servers": 1, "tools": 1},
    # The Host's own database: describe reads the catalog of `world`, history
    # lists snapshot files. sql and restore change it, so they take the tool's
    # full mask.
    "data":                 {"describe": 1, "history": 1},
}


# ── A remote MCP server's tool (server/mcp_connections.py) ──────
# `mcp:<server>/<tool>` is not a registry name: a server's tools come from its
# own list. Every such call is authorized against this one row — external_api
# (32), because the call leaves the pod through the egress gate to a server
# the person connected — after the turn type's `mcp:<server>` key
# (turn_types.check_call). A name that is not mcp:<server>/<tool> gets no row
# and is denied like any unregistered name.
MCP_TOOL_CAPS: dict = {"required_bits": 32, "domain": "external.operator",
                       "desc": "call one tool of an MCP server the person connected and approved"}


def mcp_tool_caps(name: str) -> dict | None:
    """The capability row for `mcp:<server>/<tool>`, or None."""
    from mastercode.runtime.mcp_names import parse_tool_name
    return MCP_TOOL_CAPS if parse_tool_name(name) is not None else None


# ── A function the Host wrote in its own database ──────────────
# `world:<name>`. One row
# for all of them, like MCP_TOOL_CAPS, with `data`'s own mask: it reads the
# Host's records (1|2) and may write its own schema (4), as host_world. No
# external_api: a function in the database reaches nothing outside it.
WORLD_TOOL_CAPS: dict = {"required_bits": 1 | 2 | 4, "domain": "core.entity",
                         "desc": "call one function the host wrote in its own database"}


def world_tool_caps(name: str) -> dict | None:
    """The capability row for `world:<name>`, or None."""
    from mastercode.world_db import parse_tool_name
    return WORLD_TOOL_CAPS if parse_tool_name(name) is not None else None

# The parameter each tool carries its op in. Everything uses `op`; the few
# that use `action` are listed explicitly rather than guessed at call time.
OP_PARAM: dict[str, str] = {
    "build": "action", "image_generate": "action", "artifact": "op",
    # context's op parameter is `action`, not the default `op`. Declared now so a
    # future READ_SAFE_OPS entry resolves the right key; see the note there.
    "context": "action",
}
DEFAULT_OP_PARAM = "op"


def op_param_for(tool: str) -> str:
    return OP_PARAM.get(tool, DEFAULT_OP_PARAM)


# ============================================================================
# Helpers
# ============================================================================

def scopes_to_bits(scopes: Iterable[str]) -> int:
    """OR each scope string's bit value. Unknown scopes contribute 0.

    This is the bridge between the app-side scope-string language and
    the 8-bit mask. Platform-domain scopes never appear in
    ENTITY_SCOPE_TO_BITS, so they always contribute 0 → callers asking
    for platform bits in the entity domain default-deny structurally.
    """
    mask = 0
    for s in scopes:
        mask |= ENTITY_SCOPE_TO_BITS.get(s, 0)
    return mask


async def _resolve_entity_scopes(pool: asyncpg.Pool, entity_id: int, *,
                                 strict: bool = False) -> set[str]:
    """Union the implicit scope grants from each of the entity's
    entity_domain_access rows. Returns the entity's effective scope set
    in its OWN CEIGAS domain.

    No row for a domain → no scopes from that domain. No platform scopes
    can leak in because they're not in DOMAIN_ACCESS_TO_SCOPES at all.

    A failed READ returns the empty set — the capability gate then denies,
    which is the fail-closed answer. `strict=True` re-raises instead, for a
    caller that has to SAY why it denied: an empty set and an unreadable
    store are the same bits and different sentences ("this host holds none
    of …" vs "could not be read this call"), and the MCP gate told a caller
    the first when the second was true.
    """
    try:
        rows = await pool.fetch(
            "SELECT domain_id, access_level FROM entity_domain_access "
            "WHERE entity_id = $1",
            entity_id,
        )
    except Exception as e:
        log.warning("entity_domain_access query failed for entity %d: %s", entity_id, e)
        if strict:
            raise
        return set()

    scopes: set[str] = set()
    for r in rows:
        key = (r["domain_id"], r["access_level"])
        scopes |= DOMAIN_ACCESS_TO_SCOPES.get(key, set())
    return scopes
