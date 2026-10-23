"""mastercode.runtime.direct_ops — the deterministic ACT lane.

The WRITE-side sibling of the READ/ACT split (`tool_access.is_read_safe_tool`).
A large class of mutations is a SINGLE deterministic call whose arguments the
host already holds when it decides to act: revoke NAC rule #12, quarantine
bastion 3, restart an app, flip a role's model route. Routing those through a
dispatched agent put a second model between the host's decision and one
schema-validated call — token overhead plus a transcription error surface
(wrong id, wrong op) that bought nothing, because the judgement had already
happened in the host's turn (interpretation is the host's
job; executing an already-interpreted config is not a model's job).

`operate` executes exactly one registry tool call through the SAME walls a
dispatched agent would hit — nothing here loosens:

  - the code-execution wall: a code-exec tool (the host's own build doors and
    the retired code tools, `code_exec_guard._is_code_exec_tool`) is refused
    structurally and routed to the host's own doors;
  - the CEIGAS capability gate: `_execute_meta_tool` still
    authorize against the entity's bits, log to entity_behavioral_log, and
    honor the kill switch — `operate` cannot reach anything `dispatch`
    couldn't;
  - the in-route user gates: NAC widens still demand the user's TOTP step-up,
    approve/sign stays a user-only route. Removing the model from the loop
    removes ITS errors; it never removes a gate.

Deny-by-default: a tool not in DIRECT_OP_TOOLS is refused here, and the
refusal names the lane tool_lanes.lane_for gives it — membership is a ROUTING
classification (may this run without a model?), never an authorization (the
caps gate owns that), so a wrong entry mis-routes a call but cannot widen what
the entity may do.

Every in-platform tool is listed here explicitly (the
Host calls an agent only for untrusted work outside the platform; everything
in-platform it does itself). The agent lane is the closed list
tool_lanes.UNTRUSTED_WORK_TOOLS, and an unlisted tool is a `gap` that
scripts/check_tool_lanes.py fails on — it is never sent to an agent by default.
"""
from __future__ import annotations

import logging
from mastercode.runtime.tool_call_ledger import refused as _refused

log = logging.getLogger("cseries.mastercode.direct_ops")

# ── The deterministic-op registry ───────────────────────────────────────────
#
# Inclusion rule: every tool that acts inside the platform — on
# the Host's own state, its own apps and machines, its user's stores, the
# platform's own pipelines — is listed here, whatever it takes to run. The
# question this set answers is "may the Host run this itself", and for
# in-platform work the answer is yes (the Host calls an agent
# ONLY for untrusted work outside the platform). A slow op is not a reason to
# dispatch: an agent waits just as long and adds a model call. A model inside
# the tool is not a reason either: model_bench runs a whole matrix here.
#
# Deliberately NOT here (routing, not authorization):
#   - the host's own code doors (build, ladder.run, review.compare /
#     propose_commit / adopt, mesh_harness_run) — code_exec_guard
#     .HOST_DOOR_TOOLS; the code-exec wall refuses them here structurally.
#   - untrusted work: tool_lanes.UNTRUSTED_WORK_TOOLS (github). Its reads are
#     the Host's own (read_safe_ops, the turn is marked untrusted); the rest
#     is an agent's.
#   - reads: web_search, read_url, research and the other read-lane tools are
#     reached through `read`, never `operate`.
#
# THE WALLS THAT ARE NOT THIS SET. Absence from this set was once argued as a
# wall for the credential tools (keys_manage, connect_compute,
# reachout_channel): "run_direct_op refuses a name not in this set, so no
# scheduled op_tool can reach it". The scheduling wall is UNATTENDED_OP_TOOLS,
# checked at fire time by the offline runner (offline_task_runner._run_op)
# before run_direct_op is reached, and none of those tools is in it. The
# other walls live in the tools themselves and are unchanged by who calls:
#   - reachout_channel: creating an http_call channel binds a sealed credential
#     to a destination, and every later fire happens with nobody present — the
#     creation is the widen, so it demands the user's step-up code in
#     server/reachout/service.add_channel.
#   - keys_manage: `use` brokers a credential to a URL the call names, so it
#     is refused on a turn that read untrusted content (runtime/turn_taint).
#   - mgmt_run: a write on a machine is refused on a marked turn, and the
#     machine's own grant and the person's tap still gate it.
#   - machine_exec: the root turn, the machine's standing grant, the taint.
#
# WHY THE MIXED-OP TOOLS ARE HERE. circles_manage, contacts_manage,
# host_message, context_ingest, workspace_file, project_phase,
# archive_project, the publish/ship tools, import_repo and
# integrations_manage were once left out, "pending a follow-up sweep once
# each op enum is read from its live schema". Measured on one Host: EVERY
# agent dispatched over 14 days (6 agents, 15 opus-5 model calls) did one
# deterministic bookkeeping call from exactly that list —
# project_phase add_phase, workspace_file write,
# ship_project_app + publish_site + manage_site. A model was paid to retype
# arguments the host already held, which is what this lane exists to stop
# (the ops are deterministic and static functions that don't need LLMs;
# there should only be model calls when there's reasoning to be done).

DIRECT_OP_TOOLS: frozenset[str] = frozenset({
    # ── network / bastion governance (drafts inert; widen = user step-up
    #    in-route; the host can never approve its own proposal) ──
    "propose_egress_rule", "propose_mgmt_grant", "propose_nac_policy",
    "approve_nac_draft", "revoke_nac_rule",
    "quarantine_bastion", "suspend_bastion", "manage_bastion",
    # root commands on a machine whose bastion granted them: ONE command with
    #    args the Host
    #    already holds. The machine's standing grant, the turn's taint state
    #    (runtime/turn_taint) and the agent wall gate it; see _exec_machine_exec.
    "machine_exec",
    # mgmt_run: one granted op on a server in the user's mesh,
    # under the user's signed grant, with the person's tap for a write — the
    # same class as machine_exec. A write is refused on a turn that read
    # untrusted content (tools._exec_mgmt_run); it is not schedulable.
    "mgmt_run",
    # pairing grants on an anchored machine: one signed record
    # each; approve's widen wall is the user's step-up in the service
    "manage_pairing",
    # ── own-pod + app lifecycle ──
    "system.restart_app", "system.start_app", "system.stop_app",
    "install_app", "uninstall_app", "refresh_app_store", "purchase_app",
    # open_app: opens one of the user's apps on their screen — an ACTION with
    # a read capability bit. One deterministic call, so it runs here, not
    # through a dispatched agent.
    "open_app",
    "manage_site", "manage_site_domain",
    # ── governance / bookkeeping ──
    "decision_resolve", "ladder.decision", "tickets",
    # memory_scope: one UPDATE of a preference's scope from the
    # id + scope the host supplies — no model in the tool, reversible (the
    # prior scope stays on the row).
    "memory_scope",
    # memory_curate (memory ops are plain tool calls): add, replace or drop one row from the text and id the host
    # supplies. Its add/update/delete classifier (a second model) is gone,
    # so it is a single-call, schema-complete mutation like memory_scope.
    "memory_curate",
    "schedule_task", "task_template", "entity_tasks",
    # ── project row — one INSERT from a name + description the
    #    host already holds (tools_projects.py:_exec_create_project: no op enum, no
    #    content generation; the started-from marker is bookkeeping). It was
    #    dispatch-only, so "make a scratch project, then build" cost an agent
    #    whose grant the host had to get exactly right before it could even
    #    reach `build`. Same caps gate as every op here. ──
    "create_project",
    # ── the admission bench — op='run' drives the fixed matrix
    #    against one candidate on the user's key and records the verdict;
    #    status/list/suite are reads (read_safe_ops). One deterministic call
    #    with the (provider, model) the host already holds, no model judging
    #    a model, and it spends the user's money — so it is an `operate` op:
    #    the host states an intent, and the skill says to name the cost first.
    "model_bench",
    # ── config flips (validated enums / typed rows) ──
    "credential_wiring", "model_route", "embedding_route",
    "set_project_routing", "context_backend", "phone_alias",
    # ── coding protocol files — save/delete a validated JSON
    #    document the host and user agreed in conversation, record a run's
    #    outcome on the user's say. list/get/status are reads (read_safe_ops).
    #    The tool RUNS nothing: the host drives a protocol through its own
    #    doors (build / ladder.run), which stay host-only. ──
    "coding_protocol",
    # ── a project's build pipeline — status is a read
    #    (read_safe_ops); propose is ONE deterministic call that files a
    #    decision ticket the user answers. The tool never writes the setting. ──
    "pipeline",
    "turn_type",
    # ── the person's limits — status is a read; propose files a card the
    #    person decides. The tool never writes a limit. ──
    "limits",
    # ── gitea — list/repo/tree/read/grep are reads
    #    (read_safe_ops); op='propose' is ONE deterministic call with the
    #    files the host already holds: fork, branch, one commit, pull request
    #    upstream (server/routes/gitea_propose.py). No model in the tool, no
    #    code run, and the upstream never receives a push. Without this entry
    #    the write op would send the host through a dispatched agent to file
    #    a PR it had already written. ──
    "gitea",
    # ── the bug cache — every op is one bridge round-trip with
    #    args the host already holds (a state, a number, a url): pending /
    #    validated lists, read of a validated report, fix_open. The verdict
    #    op is refused outside a dispatched agent by the tool itself, so
    #    `operate` can never validate a report. No model in the tool. ──
    "bug_review",
    # ── the support queue's collect — one bridge read and one
    #    ticket insert per received row, args the host already holds (none).
    #    The verdicts are not a tool at all: the platform applies the user's
    #    decide (server/routes/tickets.py), so no model can send one. ──
    "support_collect",
    # ── the taste lane — the user states a preference in
    #    conversation and it lands in their own profile. Writing down what
    #    someone just told you is not an operation that deserves an agent
    #    round-trip; list/tokens_get are reads (read_safe_ops). ──
    "preference",
    # ── the explanation level — same shape as `preference`: the
    #    user asks in words ("less detail"), the host writes ONE enum key of
    #    behavioral_params through the pane's own service. get is a read
    #    (read_safe_ops); set is one validated call with args the host
    #    already holds. Without this entry `set` was refused in the host's
    #    turn and routed to an agent, which the tool's own description and
    #    the Circles manifest promised would not happen. ──
    "explanation_level",
    # ── harness sign-in — runtime/tools_projects.py:_exec_harness_connections.
    #    list/matrix/status are reads (already direct via read_safe_ops);
    #    login / login_code / login_status are each ONE call the host makes
    #    with args it already holds: start the VENDOR'S OWN login in the
    #    harness pod and hand back its URL, relay the code the user copied off
    #    the vendor's page, ask whether it finished. Nothing is minted or
    #    stored platform-side (the vendor binary writes its own grant; the
    #    route persists what the pod wrote, same as before). The dashboard's
    #    Connect card seeds exactly this ask ("Run its own login …"), but the
    #    host could not run it: the name is not read-safe, was not here, and
    #    the dispatch it fell back to needed a grant it had to get exactly
    #    right ("missing read_context", and the claude code login flow
    #    broke). Reads ⊆ operate: `operate` with a
    #    read op is harmless, and the caps gate still owns authorization. ──
    "harness_connections",
    # ── reach-out — how the host gets its person's attention
    #    when work finishes while they are away. Every op is ONE typed row or
    #    ONE fire from arguments the host already holds when it decides to act:
    #    list is a read (read_safe_ops), add_channel/add_trigger write a
    #    validated row, test fires one channel once, remove deletes by id. No
    #    model is in the tool and no code runs — and the transport that DOES
    #    reach outward, http_call, goes through the vault broker, whose own
    #    gates (a live credential, the governed egress proxy, a vault_audit
    #    row) are unchanged by running here. The alternative was a dispatched
    #    agent to write down a phone number the user had just said out loud.
    #    BUILDING a channel is NOT this tool — see reachout_channel in the
    #    exclusion list above. What is left here cannot point a credential
    #    anywhere new: list reads, add_trigger subscribes an EXISTING channel,
    #    test and remove_* act on one the user already approved. ──
    "reachout",
    # ── single bridge writes (content is host-authored in the same turn) ──
    "inbox_send", "calendar_log",
    # ── the weekly "state of the host" changelog: reads
    #    five of the host's own tables since its last run and writes ONE inbox
    #    row. Deterministic, no model; the platform default standing op every
    #    host is seeded with at boot (runtime/host_changelog.py). ──
    "host_changelog",
    # ── browser agents — a task the USER recorded in the Browser
    #    app, replayed by server/browser_agents.py through the sidecar's
    #    sessions lane. No model anywhere in it: the host's part is to name
    #    the agent (run / resume with the user's answer / list / runs). The
    #    same caps gate applies; the vault still types every credential. ──
    "browser_agent",
    # ── the generic kernel-route surface (kernel_ops.py) —
    #    deny-by-default exposure + per-call scope bits enforced inside ──
    "kernel_op",
    # ── artifacts — the host's OWN sketches: create / write /
    #    set_desktop / promote / import / revert / delete each write files the
    #    host authored in THIS turn into its own workspace, and every write is
    #    a revision it can revert. Same shape as inbox_send: content is
    #    host-authored, the call is one schema-complete write, no model in
    #    the tool. Its own description said "call this tool YOURSELF, NEVER
    #    dispatch an agent to make one" while the lane table put it in
    #    dispatch (create/write/delete ∈ _WRITE_OPS), so the host held only
    #    op='list' and dispatched an agent to copy bytes it had already
    #    designed — three tasks, 40k-70k tokens each. The read
    #    op stays a direct read (read_safe_ops); the rest run here. ──
    "artifact",
    # ── the host's own index — embed_jobs op='enqueue' inserts
    #    ONE job row from args the host already holds (a bank name, or a
    #    repo the user just said yes to) and index_my_code kicks one
    #    background walk of a tree the host owns. Neither runs a model in the
    #    tool; both were in the dispatch lane, so "yes, embed that repo" would
    #    have cost an agent whose only act was the same one-line call.
    #    coverage/list/get stay direct reads (read_safe_ops). ──
    "embed_jobs", "index_my_code",
    # ── the oversight ledger — runtime/oversight.py. One
    #    pass over every bastion's LATEST heartbeat, folding the summaries the
    #    beats already carry (alerts, detect, posture, offline) into
    #    oversight_findings. No model anywhere in it (no model
    #    in the sensing path), no content generation, no remote run: it reads
    #    rows this pod already holds and writes rows to a table it owns. Same
    #    shape as host_changelog, which is why it sits beside it there too. ──
    "oversight_ingest",
    # ── the project row's whole surface — `create_project` alone here would
    #    let a host make a project without a model and then need one to name
    #    a phase in it.
    #    Each is one entity-scoped service call with arguments the host is
    #    holding when it decides to act:
    #      project_phase        list / add_phase / set_phase_spec /
    #                           archive_phase / add_step / set_step_status —
    #                           SELECTs and single INSERT/UPDATEs against
    #                           project_phases + phase_steps, with the
    #                           acceptance/gate validators running before the
    #                           write (tools_projects.py:_exec_project_phase). This is
    #                           the single most-dispatched op measured.
    #      archive_project      one UPDATE to status='archived'; nothing is
    #                           destroyed.
    #      update_project       rename / re-status / re-style; only the fields
    #                           passed change. repo= mounts a repository onto
    #                           a TICKET's project (v199): one forge read of
    #                           the Host's own access, one UPDATE, and the
    #                           code read in the background, never in the turn.
    #      set_project_manifest writes app.yaml and rolls it back if the
    #                           validator refuses, so an invalid manifest is
    #                           never persisted.
    #      project_adopt_thread retags turns + their memories into a project
    #                           and leaves a marker turn behind. A move, not a
    #                           delete.
    #    get_project / get_project_activity are NOT here and need nothing: both
    #    are whole-tool reads already (the get_ prefix), same as list_skills. ──
    "project_phase", "archive_project", "update_project",
    "set_project_manifest", "project_adopt_thread",
    # ── the workspace + the ship lane ──
    #    workspace_file   list/read are reads (read_safe_ops); write/delete are
    #                     one sandboxed file op each inside $CEIGAS_WORKSPACE or
    #                     a project dir the host owns, with the path-traversal,
    #                     review-dir and licence write-gates all inside the
    #                     executor. Every write commits to the project's
    #                     host/build branch, so the provenance is kept whoever
    #                     calls it.
    #    ship_project_app pack + sign + install the host's OWN project. Every
    #                     invariant (provenance=custom, the build-contract
    #                     validator, the sign-time App-SDK integrity gate) lives
    #                     in ship_project_service and is unmoved by the caller.
    #    publish_site     one publish_site_service / publish_site_repo_service
    #                     call; a NEW site goes live public, as
    #                     manage_site's publish always has.
    #                     The user asked for it, and publish:sites authorizes
    #                     it -- this list only routes the call.
    #    publish_app      my_apps/sales/agreement/requests are reads;
    #                     set_price/yank/publish are single caller-scoped bridge
    #                     ops. The bridge fixes the publisher from its mTLS cert
    #                     — a host can only ever set its own price or yank its
    #                     own version — and op='publish' SUBMITS to a
    #                     human-reviewed queue, so success is a pending request
    #                     rather than a live listing. The user's acceptance of
    #                     the publishing agreement is still a required argument
    #                     (agreement_version); running without a model does not
    #                     supply it, it only stops a model retyping it.
    "workspace_file", "ship_project_app", "publish_site", "publish_app",
    # ── import a codebase — `git clone --depth 1` with no
    #    shell (create_subprocess_exec, GIT_TERMINAL_PROMPT=0, 180s timeout)
    #    into a project dir that must be empty, rolled back on any failure.
    #    NOTHING from the cloned tree is executed: it is a fetch, and the
    #    code-exec wall is untouched.
    #
    #    It is the SLOWEST op in this registry and the only one that can hold
    #    the host's turn for minutes — the timeout is 180s and a large repo
    #    reaches it. That is not a reason to dispatch it: an agent would wait
    #    the same three minutes and add a model call to the bill. It IS a
    #    reason to warn the person, so the tool's own description says the
    #    clone may take up to 3 minutes and that the host says so before
    #    calling it. And it is LOCAL — dcn op='run_plan' below hands a job to
    #    someone else's machine; this one fetches onto our own disk. ──
    "import_repo",
    # ── talking to people — the same shape as inbox_send,
    #    here since the lane was built: the CONTENT is host-authored in the turn
    #    that calls it, and the call is one schema-complete write.
    #      host_message     send / conversations / thread / open over the DM
    #                       service the /api/messages routes use.
    #      circles_manage   the three reads plus create_circle and
    #                       send_circle_message.
    #      contacts_manage  the address book and the connection-request verbs.
    #                       request_peer's authorization IS the peer list (only
    #                       a host one of your machines currently serves
    #                       qualifies) and accept_request's is the inbound
    #                       request — neither is loosened by the caller.
    "host_message", "circles_manage", "contacts_manage",
    # ── the host's own shelves ──
    #    links               save/list/search/get/remove over entity_links.
    #    model_routing      status reads; recommend reads the shipped
    #                        recommended-routes.json and the live catalog and
    #                        RETURNS a suggestion (it writes nothing); set goes
    #                        through _host_route_write, the same door
    #                        model_route op='switch' takes, so the route-change
    #                        gate still decides whether it applies, no-ops,
    #                        reverts, or becomes the user's decision.
    #    integrations_manage four provider/connection reads plus revoke. Revoke
    #                        only TIGHTENS — the same asymmetry that keeps
    #                        autofill's revoke here and its grant out.
    #                        CONNECTING a provider is not this tool: OAuth
    #                        authorize/callback and api-key connect are browser
    #                        and credential flows its executor does not handle.
    "links", "model_routing", "integrations_manage",
    # ── task_record — the task's shared record. Every op writes
    #    or reads the project's own record behind the 1|2|4 mask; a finding is
    #    kept only after code finds its quote in the source the platform read.
    "task_record",
    # ── MCP connections —
    #    servers/tools are reads (read_safe_ops); connect writes ONE draft row
    #    and drafts ONE egress allow rule through the propose path, and contacts
    #    nothing: the person's approval in the Network app, with their code, is
    #    what connects it. disconnect only tightens. A server's own tools
    #    (mcp:<server>/<tool>) are not in this set and need not be: they run
    #    through run_direct_op's MCP branch below, never through `read`. ──
    "mcp_connections",
    # ── data — the Host's own
    #    database. Every op runs as host_world (mastercode/world_db.py), a role
    #    that writes nothing outside the schema `world` and reads no secret, so
    #    the wall is the database's, whatever the SQL says. describe/history
    #    are reads (read_safe_ops); sql and restore change only `world`, and a
    #    snapshot is taken before anything that can remove data. ──
    "data",
    # ── autofill — designed to be model-safe and says so: no
    #    op can return a saved VALUE (`saved` returns keys and a password flag,
    #    `fill` returns which refs were filled), the same promise keys_manage
    #    makes. The WIDEN is not here and cannot be — op='grant' is refused
    #    inside the executor because creating a standing grant is the user's own
    #    act in the Keys app — and revoke only tightens. So
    #    what runs here is: read what is saved, type the mapping the host worked
    #    out from a page snapshot, or stop unattended use. Deliberately NOT in
    #    UNATTENDED_OP_TOOLS: filling needs a browser session someone opened. ──
    "autofill",
    # ── report_crash — one bridge call
    #    (platform.report_crash) carrying component, version, signal, exit code
    #    and the SIZE of any dump. Never the dump: crash artifacts are the
    #    user's data, surfaced to them and never collected. The consent is the
    #    user's yes in the turn before it, which is a sentence the host says —
    #    putting a second model between that yes and one metadata POST adds
    #    nothing. Repeats inside 24h fold into one report. ──
    "report_crash",
    # ── context_ingest and dcn — PER-OP, see
    #    DIRECT_OP_EXCLUDED_OPS below.
    #      context_ingest  stage / bulk / search / promote / list / files /
    #                      read / extract / extract_machines are reads and
    #                      single service calls against user_context and
    #                      entity_memory; extract reads a stored file (in the
    #                      host, or on a machine for OCR / speech).
    #      dcn             stores / status / list / stat / read_range / search /
    #                      catalog / plans are reads, and propose_store /
    #                      propose_plan create INERT DRAFTS the user approves
    #                      with their step-up code — the same shape as
    #                      propose_nac_policy, here since this lane was built.
    #                      op='run_plan' is excluded — it moves files on the
    #                      user's machine.
    "context_ingest", "dcn",
    # ── in-platform tools that were agent-only by default ──
    #    Before the agent lane became a closed list, a tool not named here
    #    fell to `dispatch`. These act only inside the platform:
    #      context            the Host's OWN context files, its skills
    #                         included (host_skills: it writes skills with
    #                         context(action='write')).
    #      image_generate     a picture on the user's own image route from a
    #                         prompt the Host wrote; it spends the user's
    #                         money, so it is not unattended.
    #      keys_manage        the Host's own vault; no op returns bytes. `use`
    #                         is refused on a marked turn, in the executor.
    #                         `add` and `seal_for_user` stay direct: both
    #                         seal a value the Host itself
    #                         holds. `add` seals it to the Host (the forge
    #                         token of a server it built, a mirror's token,
    #                         a context backend's S3 keys); `seal_for_user`
    #                         seals it to the user's registered key, so the
    #                         Host hands over something it made and can never
    #                         reopen it (server/vault/api.seal_for_user). A
    #                         secret the person owns enters through the Keys
    #                         app, which needs no Host op.
    #      connect_compute    register / assign / revoke / run on the Host's
    #                         own bring-your-own compute.
    #      reachout_channel   building a channel to the Host's own person;
    #                         the step-up code is demanded in the service, and
    #                         it is not unattended.
    #      propose_capability one filing into the platform's proposals
    #                         pipeline, in the turn after the user agreed.
    #      community_board    the platform's own board. Posting is the Host's
    #                         own words; what it reads there is other members'
    #                         words and marks the turn (runtime/turn_taint).
    #      state_store        one write to the Host's own context KV.
    #    A change to the platform source is gitea op='propose' (above); the
    #    second path to it was retired (tool_lanes.RETIRED_PROPOSE_TOOLS).
    "context", "image_generate", "keys_manage", "connect_compute",
    "reachout_channel", "propose_capability", "community_board",
    "state_store",
})

# ── The per-op exclusions ──────────────────────────────────────
#
# DIRECT_OP_TOOLS is whole-tool, and for nearly every entry above that is the
# right grain: reads ⊆ operate, so a mixed read/write tool goes in whole. An op
# named here is the exception: it reads content someone outside the Host wrote
# and is an agent's work, while the rest of the tool runs here.
#
# ROUTING, exactly like membership above: an op named here is refused by
# `run_direct_op` and routed to `dispatch`. It is never an authorization — the
# CEIGAS caps gate owns that and is unchanged, so a wrong entry mis-routes a
# call and cannot widen what the entity may do.
DIRECT_OP_EXCLUDED_OPS: dict[str, frozenset[str]] = {
    # context_ingest op='extract' is not in this set (it is a read
    # function), and neither is dcn op='run_plan': applying a move plan the
    # user approved, on the user's own
    # machine, is in-platform work like machine_exec. An agent waits on the
    # same job and adds a model call; it adds no isolation.
    # The bug cache: `read` of a pending report and `verdict` are
    # the validator AGENT's ops — a model reads a report and judges it. They
    # are refused in the host's own turn by the tool itself, and naming them
    # here is what lets the host DISPATCH that agent: with every op of
    # bug_review classed deterministic, dispatch_precheck's clerk rule
    # ("run these yourself with operate") refused the validator before it
    # started. pending / validated / fix_open stay operate ops.
    "bug_review": frozenset({"read", "verdict"}),
}

# The selector keys a registry tool may use for its op enum — the same list
# tool_access._schema_op_tokens walks, so the two cannot drift on a tool that
# names its selector something other than `op`.
_OP_ARG_KEYS = ("op", "action", "operation", "method", "command", "mode")


def direct_op_excluded_ops(name: str) -> frozenset[str]:
    """The ops of `name` that do NOT run through `operate` (empty for most)."""
    return DIRECT_OP_EXCLUDED_OPS.get((name or "").strip(), frozenset())


def is_fully_direct_op_tool(name: str) -> bool:
    """True iff EVERY op of `name` runs through `operate`.

    The predicate the dispatch door needs. `is_direct_op_tool` answers "can
    this tool run without a model", which is true of `bug_review` while its
    read/verdict ops are the validator agent's; refusing a dispatch on that
    weaker predicate would refuse the one dispatch that tool legitimately
    needs."""
    n = (name or "").strip()
    return n in DIRECT_OP_TOOLS and not DIRECT_OP_EXCLUDED_OPS.get(n)


def excluded_op_refusal(name: str, args: dict | None) -> str | None:
    """The refusal text when `args` names an op outside this lane, else None.
    Reads the op from whichever selector key the tool uses."""
    excluded = direct_op_excluded_ops(name)
    if not excluded:
        return None
    args = args if isinstance(args, dict) else {}
    for key in _OP_ARG_KEYS:
        val = args.get(key)
        if val is None:
            continue
        if str(val).strip().lower() in excluded:
            return (
                f"`{name}` op='{str(val).strip()}' reads content another Host "
                "or person wrote, so it is an agent's work and does not run "
                f"through `operate`. Dispatch an agent with {name} in "
                "tool_scope and work='message' for this one. Every other op "
                f"of `{name}` runs here."
            )
    return None


def _gap_text(name: str) -> str:
    return (f"`{name}` has no door yet: it is neither a read nor an operation the "
            "platform lists. Nothing ran. This is a gap in the platform, and an agent "
            "is only for untrusted work outside it, so say what you could not do and "
            "file it: gitea op='propose' on the platform repo if you hold the "
            "platform source, else tickets op='support_open'.")


def lane_route(name: str, schema: dict | None = None) -> tuple[str, str | None]:
    """How the Host reaches a known tool `name`: one sentence naming the call,
    and the door to use instead (None when nothing runs it).

    Every refusal of a tool named at the wrong door (operate's, the read
    door's, a call by name) answers from here, with the lane
    tool_lanes.lane_for gives the tool, so none of them can send in-platform
    work to an agent: only a `dispatch`-lane tool, untrusted work outside the
    platform, is pointed at one. Three separate fallbacks would each offer
    "dispatch an agent" for anything not in DIRECT_OP_TOOLS (e.g. app_probe,
    image_generate, agent_status)."""
    from mastercode.runtime.tool_lanes import TURN_VERBS, lane_for
    from mastercode.runtime.tool_call_ledger import known_tool_schema
    n = (name or "").strip()
    if n in TURN_VERBS:
        return f"`{n}` is one of your verbs: call `{n}` directly by its name.", n
    lane = lane_for(n, schema if schema is not None else known_tool_schema(n))
    if lane == "read":
        return f"`{n}` is a read: call it with read(tool='{n}', args={{...}}).", "read"
    if lane == "operate":
        return (f"`{n}` is an operation you run yourself: operate(intent, tool='{n}', "
                "args={...}), one call in your own turn, and the result returns to you "
                "now."), "operate"
    if lane == "host":
        return f"`{n}` is one of your own build tools: call `{n}` directly by its name.", n
    if lane == "dispatch":
        return (f"`{n}` is untrusted work outside the platform: its reads are yours "
                f"through read(tool='{n}', ...); its other ops go to an agent with "
                "dispatch, naming the work."), "dispatch"
    if lane == "retired":
        return f"`{n}` is retired; nothing runs it.", None
    return _gap_text(n), None


def not_an_op_refusal(name: str) -> str:
    """What `operate` says about a name outside DIRECT_OP_TOOLS: the door that
    works, from lane_route. An operate-lane name that still reached here (in
    the lane table but not in the registry) is a gap, never a loop back to
    operate."""
    text, use = lane_route(name)
    return _gap_text(name) if use == "operate" else text


def is_direct_op_tool(name: str) -> bool:
    """Routing predicate for the catalogs/prompt: may this tool run through
    `operate` (no model in the loop)? Never consulted for authorization."""
    return (name or "").strip() in DIRECT_OP_TOOLS


# ── The unattended subset — a FITNESS classification ───────────
#
# A THIRD question, orthogonal to the two above it:
#
#   DIRECT_OP_TOOLS      may this run without a MODEL?     (routing)
#   the CEIGAS caps gate may this entity do this at all?   (authorization)
#   UNATTENDED_OP_TOOLS  does this complete without a PERSON?   (fitness)
#
# It is NOT a safety gate and must never be argued for as one. A standing op is deterministic, runs no model, sits inside a
# granted scope, and exists because the user already asked for the outcome —
# building the automation IS the answer to that request, so there is no
# approval step and no confirm prompt. Authorization stays where it already
# lives: the caps gate, at fire time, fail-closed.
#
# The only reason to keep an op off a schedule is that it does not WORK with
# nobody there, because its result is something a human has to act on:
#   - open_app "opens one of the user's apps on their screen" — at 03:00 it
#     opens a window nobody sees.
#   - harness_connections op='login' hands back the vendor's login URL for the
#     user to visit — unattended, that URL goes into the void.
#   - anything that trips a user step-up gate stalls on a prompt nobody will
#     answer, so the op cannot complete.
# Scheduling those is not dangerous. It is useless, which is a different and
# much cheaper thing to reason about.
#
# THE SET IS EXPECTED TO GROW. An op missing here is a capability the host does
# not have, and this lane exists to give the host capabilities. The bar for
# adding one is a read of what it actually does when it returns — not a risk
# assessment. Obvious next candidates, deliberately left out until someone
# reads them: refresh_app_store, model_route, embedding_route, context_backend,
# set_project_routing, decision_resolve, tickets.
#
# Seeded with reads, the host's own bookkeeping, and notifications to its user.

UNATTENDED_OP_TOOLS: frozenset[str] = frozenset({
    # Notify the user. The whole point of a monitor: look, then say something.
    "inbox_send",
    # The bug cache's standing poll: op='pending' with wake=true
    # looks, and when reports are waiting and no validation turn is queued it
    # schedules ONE — a task it owns outright, completes with nobody present.
    # With nothing waiting it returns and costs nothing. The other ops are
    # equally self-completing (a listing, a read, a mark).
    "bug_review",
    # The support queue's standing collect: reads the queue,
    # raises the missing cards, returns. Nobody present is the whole point —
    # the cards wait for the person in the Tickets pane.
    "support_collect",
    # Look at the host's own week, then say something — reads + one inbox row,
    # completes with nobody present by construction.
    "host_changelog",
    # The host's own bookkeeping — records and schedules it owns outright.
    "calendar_log", "entity_tasks",
    # Reach-out is NOT here, on this set's OWN criterion rather than on a
    # safety argument,
    # because this set is a fitness classification and the header above is
    # right that it must never be argued for as a gate.
    #
    # Read of what `reachout` op='test' does when it returns: it fires a
    # channel with a synthetic "This is your Host — reach-out test" message and
    # hands back the log row's status. Its RESULT is something a human has to
    # act on — the design's own choreography is "run `reachout test` and tell
    # the user what should have arrived and where". With nobody there it sends
    # a text against an expectation nobody formed and nobody checks. That is
    # `open_app` exactly ("at 03:00 it opens a window nobody sees"), and it is
    # the criterion this set states.
    #
    # What made someone look was the cost, and it belongs in the record even
    # though it is not the argument: `test` deliberately ignores the cool-down
    # (server/reachout/service.test_fire), so schedule_task(op_tool='reachout',
    # op_args={op:'test'}, recurrence='every:30') would fire a paid channel at
    # the user's own SMS provider ~2,880 times a day. Useless AND expensive,
    # which is a worse combination than either.
    #
    # The set is whole-tool, so this costs a scheduled `list` — the only op
    # here that would genuinely work unattended. Nobody has asked for one, and
    # if anyone does the fix is to split the reads out under their own name,
    # not to re-admit `test` alongside them.
    # Standing work managing standing work: re-arm, retire, stamp a template.
    "schedule_task", "task_template",
    # The switch for a resident (always-on) process app. Read of
    # what they do when they return: start_app spawns the sidecar, waits for
    # its readiness and persists ON; stop_app terminates it and persists OFF.
    # Both finish with nobody present and hand nothing to a person — which is
    # the whole point: "start the loop at 09:00, stop it at 17:00" is two
    # scheduled ops with no model in the loop.
    "system.start_app", "system.stop_app",
    # A recorded browser agent. Read of what op='run' does when
    # it returns: wakes the browser, replays the user's own steps, saves the
    # download where the agent file says, seals the login cookies back, puts
    # the browser to sleep, and writes a run record. Completes with nobody
    # present — that is the entire reason it exists ("export my bank ledger
    # every day. I dont want an llm to do it"). The one step that can need a
    # person, `pause_if`, parks the run and writes the question to the inbox
    # (server/browser_agents._notify); a run nobody answers times out as
    # FAILED with the reason — also to the inbox — and never hangs.
    "browser_agent",
    # The oversight ledger. Read of what it does when it
    # returns: it folds the machines' own summaries into oversight_findings and
    # returns counts. Nothing about that needs a person — it is the standing
    # work the whole management lane is built around, and at 03:00 is exactly
    # when it matters. It hands nothing to anybody: the one thing it can SAY is
    # an inbox row for a new critical/high, which is a notification the user
    # reads when they wake, not a question waiting on an answer.
    "oversight_ingest",
})

# Every unattended op must also be a direct op — the fitness question only
# makes sense about something that already runs without a model. Asserted at
# import so a typo or a registry rename is a hard failure at boot rather than a
# refusal at 03:00.
assert UNATTENDED_OP_TOOLS <= DIRECT_OP_TOOLS, (
    "UNATTENDED_OP_TOOLS must be a subset of DIRECT_OP_TOOLS; stray: "
    f"{sorted(UNATTENDED_OP_TOOLS - DIRECT_OP_TOOLS)}"
)


def is_unattended_op_tool(name: str) -> bool:
    """Fitness predicate: does this op complete with nobody present?

    Consulted twice — at authoring time by `schedule_task` (UX fail-fast, so
    the host learns immediately) and again at fire time by the offline runner
    (the real check: `_exec_schedule_task` is not the only writer of a
    scheduled_task payload — POST /api/calendar wraps handle_calendar_append
    directly for the Circles compose modal and the PME path, so an authoring
    check alone is bypassable). Never consulted for authorization.
    """
    return (name or "").strip() in UNATTENDED_OP_TOOLS


async def run_direct_op(name: str, args: dict, *, entity_id: int, pool,
                        prompt_console=None, session_id: str | None = None,
                        app_permissions: list[str] | None = None,
                        origin_context: str | None = None,
                        # Turn types: the calling turn's profile,
                        # handed through to _execute_meta_tool so an op run
                        # through `operate` meets the same wall as a direct
                        # call. None = untyped, unchanged.
                        turn_type: dict | None = None,
                        # The Circles thread the calling turn is in — a
                        # back-reference like origin_context, never a gate.
                        # schedule_task writes what is planned there.
                        thread_id: str | None = None) -> dict:
    """Execute ONE deterministic registry op — the shared `operate` backend
    for both host engines. Same two-branch exec path the read lane and
    scoped agents use: meta/bridge → _execute_meta_tool.
    interaction_context=None → no surface gate (these are background-caller
    ops, not a chat surface); CEIGAS still gates inside.

    `origin_context` is the calling thread, and it is deliberately
    NOT passed as interaction_context: putting the real thread there would
    switch the surface gate ON for every op that has always run without one —
    a behaviour change with a blast radius nobody asked for. This is a pure
    back-reference for an op that needs to know WHERE it was invoked from. The
    gate never reads it.

    Why it exists: schedule_task is a direct op, so a timer the host set while
    talking to someone in a project thread arrived with no idea which thread
    that was, and the fired task reported into the Circles self-thread. The
    CHAT dispatcher is the wrong place for it: schedule_task(op='create')
    never reaches it — create is a write, and the write lane is `operate`."""
    name = (name or "").strip()
    args = args if isinstance(args, dict) else {}

    # Code-execution wall FIRST, ahead of the registry check, so a code-exec
    # name gets the build-door routing hint instead of a generic refusal —
    # mirrors scoped_agent's ordering and the reasoning documented there.
    from mastercode.code_exec_guard import _is_code_exec_tool, HOST_DOOR_TOOLS
    if _is_code_exec_tool(name):
        return {
            "status": "denied",
            "result": (
                f"`{name}` runs code — that goes through your OWN build tools "
                f"({', '.join(sorted(HOST_DOOR_TOOLS))}), called directly; "
                "`operate` never runs code."
            ),
        }

    # A remote MCP server's tool, mcp:<server>/<tool>. Only `operate` reaches it — the read lane classifies it as an
    # action whatever the server claims (tool_access.is_read_safe_tool) — and
    # the caller has already required the intent. The capability gate runs
    # first, with the turn type: a typed turn must hold `mcp:<server>` in its
    # keys (turn_types.check_call), and the call needs external_api
    # (meta_tool_caps.MCP_TOOL_CAPS). Then the connection service refuses an
    # unapproved server, one whose signature or rule does not verify, one
    # with no skills/mcp-<server>, a tool it does not list, and arguments
    # that break its schema, before the client makes the call.
    from mastercode.runtime.mcp_names import is_mcp_tool_name
    if is_mcp_tool_name(name):
        return await run_mcp_tool(name, args, entity_id=entity_id, pool=pool,
                                  session_id=session_id, app_permissions=app_permissions,
                                  turn_type=turn_type)

    # A function the Host wrote in its own database, world:<name>. Only `operate`
    # reaches it, after the same gate an MCP tool passes (turn type, bits).
    from mastercode import world_db as _world
    if name.startswith(_world.WORLD_TOOL_PREFIX):
        return await run_world_tool(name, args, entity_id=entity_id, pool=pool,
                                    session_id=session_id, app_permissions=app_permissions,
                                    turn_type=turn_type)

    if name not in DIRECT_OP_TOOLS:
        log.info("DIRECT_OP_DENIED eid=%s tool=%s (not in registry)",
                 entity_id, name)
        return {"status": "denied", "result": not_an_op_refusal(name)}

    # Per-op wall: the tool is in the lane, this op is not. Checked
    # here rather than in the caller so every `operate` entry point — the chat
    # turn, a scheduled op, the offline runner — hits the same refusal.
    _op_refusal = excluded_op_refusal(name, args)
    if _op_refusal is not None:
        log.info("DIRECT_OP_DENIED eid=%s tool=%s (op outside the lane)",
                 entity_id, name)
        return {"status": "denied", "result": _op_refusal}

    from mastercode.runtime.triage import (
        _META_TOOL_NAMES, _BRIDGE_TOOL_NAMES,
    )
    from mastercode.runtime.tools import _execute_meta_tool
    if name in _META_TOOL_NAMES or name in _BRIDGE_TOOL_NAMES:
        return await _execute_meta_tool(
            name, args,
            entity_id=entity_id,
            pool=pool,
            prompt_console=prompt_console,
            session_id=session_id,
            app_permissions=app_permissions,
            interaction_context=None,
            origin_context=origin_context,
            turn_type=turn_type,
            thread_id=thread_id,
        )
    # There is no bot-lane fallthrough. Every direct op is a
    # meta/bridge tool, and check_direct_ops.py gates that structurally.
    return _refused(f"Unknown tool: {name}")


async def run_world_tool(name: str, args: dict, *, entity_id: int, pool,
                         session_id: str | None = None,
                         app_permissions: list[str] | None = None,
                         turn_type: dict | None = None) -> dict:
    """One call to a function the Host wrote in its own database, for
    `operate`: the capability gate (kill switch, turn type, bits, the
    behavioral log) on the tool's own name, then world_db.call_function on
    the host_world lane. The brain pool is used only by the gate's log."""
    from mastercode import world_db
    fn = world_db.parse_tool_name(name)
    if fn is None:
        return _refused(f"{name!r} is not a function name: world:<name>, with a-z, 0-9 and _")
    from mastercode.runtime.tools import _authorize_meta_tool
    allowed, denial = await _authorize_meta_tool(
        name, args, entity_id, pool, session_id,
        app_permissions=app_permissions, turn_type=turn_type)
    if not allowed:
        return denial
    try:
        return await world_db.call_function(fn, args)
    except Exception as e:  # noqa: BLE001 - a failure of the lane, said as one
        log.exception("world tool %s failed (entity=%s)", name, entity_id)
        return {"status": "error", "result": f"`{name}` failed: {type(e).__name__}: {e}"}


async def run_mcp_tool(name: str, args: dict, *, entity_id: int, pool,
                       session_id: str | None = None,
                       app_permissions: list[str] | None = None,
                       turn_type: dict | None = None) -> dict:
    """One call to a connected MCP server's tool, for `operate`: the
    capability gate (kill switch, turn type, bits, the behavioral log) on the
    tool's own name, then server/mcp_connections.call."""
    from mastercode.runtime.tools import _authorize_meta_tool
    allowed, denial = await _authorize_meta_tool(
        name, args, entity_id, pool, session_id,
        app_permissions=app_permissions, turn_type=turn_type)
    if not allowed:
        return denial
    from server import mcp_connections
    try:
        return await mcp_connections.call(pool, entity_id, name, args)
    except mcp_connections.McpConnectionError as e:
        return _refused(str(e))
    except Exception as e:  # noqa: BLE001 - a failure of the lane, said as one
        log.exception("MCP call %s failed (entity=%s)", name, entity_id)
        return {"status": "error", "result": f"`{name}` failed: {type(e).__name__}: {e}"}
