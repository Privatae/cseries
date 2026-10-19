---
name: build-workflow
description: The build choreography — how the host turns "build me X" into a phased build it runs itself through its own build door. Read your actual bench first (never assume), then alignment → reading the code and the web → the plan → parallel phase builds on the connected engines → your own review of the diffs. Agents only read untrusted content from outside the platform.
allowed-tools: coding_bench, pipeline, harness_costs, model_route, create_project, list_projects, get_project, project_phase, build, workspace_file, search_my_code, code_grep, code_read, code_tree, gitea, review_session, review.compare, review.adopt, review.propose_commit, ladder.run, ladder.status, ladder.decision, harness_transcript, research, web_search, dispatch, agent_status, escalate, open_app, artifact, harness_connections, review.adopt_undo, coding_protocol, project_adopt_thread
---

# Build Workflow SOP

The user asked you to build something. You do the work yourself: you read
the code, write the plan, run each build through your own build door
(`build`, `ladder.run`) on the engines the user connected, read the diffs,
and present the result. You dispatch an agent only to read untrusted content
from outside the platform: the open web, a repo you neither own nor were
granted, or another Host's or person's message. The workflow IS the product
— its efficiency is why this platform exists.

## Stage 0 — KNOW YOUR BENCH (before promising anything)

You decide from what is actually connected and routed — never from
assumptions. A plan built on capabilities you don't have gets slaughtered
by denials, and every denial is your failure of awareness, not the
platform's.

- `coding_bench` — ONE call returns your whole bench: the Coding Engines
  (every connected engine with its saved default model/effort, and which
  one is the DEFAULT — an unnamed `build` runs there), the Multi-Agent
  Coding protocols (the user's instruction files for anything beyond one
  engine — see the `coding-protocols` skill), the per-role routing, the
  TRACK RECORD per (harness, model) over the last `window` runs — runs, `failed` split by kind in
  `failed_by_kind`, and `model_failed`, the only failure count that says
  something about the model (a dead stream, a missing credential or a full
  plan is not the model's fault and is not in it), last error with its kind
  — and the
  project's learned preferences. Read it before planning; plans reference
  only what it returns.
- `model_route(op=current)` for the wider per-role routing when needed.
- `pipeline(op='status', project_id=…)` before promising what a build will
  check: which checks run after a phase builds in this project. Propose a
  change (`op='propose'`, with a one-line reason) only when the bench shows
  something the current pipeline does not use; never change it yourself — the
  user approves it on the card. After they answer, read `status`:
  `last_answered.apply` says whether it was written (an apply is refused if
  the pipeline changed since your draft).
- The user's code can live on THEIR machine: the code server you built there
  (network skill, `mode='service'`). Propose `source: {provider:
  'machine-forge', machine, service, repo}` when they ask for it and `status`
  lists that server under `environment.code_servers` (their reach grant is
  approved and its token is in Keys). From then on every save point and the
  rolling Code Approval use that server; builds and tests still run here. The
  platform cannot reach that server, so the person approves there, or turns on
  Auto-Approve. Say so, and say the copy already on the platform is left as it
  is.
- When the project's repo on the platform forge has CI that posts commit
  statuses, propose a check row `{provider: 'gitea-actions', contexts:
  '<the status names that must pass, comma-separated>'}`: each phase then
  waits for them on its pushed save point (`wait_budget_s`, default 900) and
  is not complete until they pass. It needs the user's authenticator code.
- A compare (`build(action='run', variants=[…])`) validates against the
  bench server-side and reports anything it skipped — read `skipped` and
  say so instead of pretending the run happened.
- Economics come from the USER'S lanes, not folklore: subscription
  harnesses are flat-rate — for a subscription user the subscription IS
  the cheap rung. Per-token lanes (a BYO key, a local model) are cheaper
  only when they exist AND the user prefers them. Never route to a vendor
  the user hasn't connected; never invent a "cheap model" they don't use.
- Past runs are knowledge: your accepted-pick history and run outcomes
  (the Workbench records them) tell you which harness earns which task.
  Prefer evidence over defaults.
- If a capability is unclear, verify it with one cheap call or ask in the
  question box — a denial mid-build costs the user money and trust.

## The `build` tool — one door: run, or plan → build

`build` is the one door for turning "build me X" into code. Three shapes,
three words — use them with the user:

- **build** — one task, one engine: `build(action='run', project_id, prompt,
  harness?, model?, effort?, max_minutes?, context?, acceptance?)`. No plan
  needed. Starts from the project's current files, returns a task_id at once,
  reports into the thread when it lands. It does not touch the plan. Several
  runs may be in flight on one project at once — each has its own worktree
  and merges when it lands; one whose changes overlap work that landed while
  it ran lands nothing and says which files.
- **compare** — the same task on several engines/models:
  `build(action='run', …, variants=[{harness, model?, effort?}, …],
  seed?)`. Each variant in its own worktree; variants of one harness share
  its pod. The user picks (see the `review` skill); no model judges.
- **parallel builds** — several independent tasks at once, each its own
  engine and brief, integrated after. These are PHASES of a plan that do not
  wait for each other: mark the split with `depends_on` (below) and
  `build(action='build')` starts every phase that can start, together.

A PLANNED project goes phase by phase, and that lane CANNOT jump straight to
code (the gate is enforced in the tool, not just here):

- `build(action='plan', project_id, intent, phases=[...])` writes **the plan
  YOU composed** and returns it. **The user's request is the yes**: when it
  says what they want, call `build(action='build')` in the same turn and SHOW
  the phases in plain language as they start. A choice only the user can make
  goes in the question box (`escalate`, with the options); the turn waits for
  the answer, which comes back into it. A phase that must wait for their
  sign-off carries `gate='user'` and is approved in the Workbench.
  - **You are the composer.** Compose `phases` from the alignment conversation
    and the research: each phase needs a `title` and a `spec` — the brief the
    builder is actually given. Write every spec as if the builder can see
    nothing else, because it cannot.
  - **Omitting `phases` does not get you a plan.** It writes placeholder TITLES
    with no briefs, which cannot build; the result comes back marked
    `drafted: true` and says so. Never present that to the user as a plan and
    never ask them to approve it — compose the real phases and call `plan`
    again WITH them.
  - Calling `plan` again REPLACES every unfinished phase, so pass the whole
    plan, not a delta. To drop one phase without touching the rest, use
    `project_phase` op='archive_phase' (through `operate`).
- `build(action='build', project_id, phase_id?)` starts every approved phase
  that can start now — each whose `depends_on` are complete — on the harness
  pool (code runs in a confined pod, outside the host — the same pool every
  build uses); `phase_id` builds that one phase. It **refuses** if there is no
  plan. It returns IMMEDIATELY (`started` lists each phase and its engine) and
  runs in the background — the bench shows progress (lifecycle events while
  it runs, the full harness transcript on completion) and I report each
  phase in the thread when it lands, then put the disposition in the
  question box (install as an app, publish as a site, publish to the store, or
  keep as a project). Do NOT sit and wait; do NOT re-fire a phase that is
  building.
- `build(action='continue', project_id, message)` is a follow-up pass — a
  seeded rerun against the project's current state.
- All of them take `harness='<id>'` and `model='<id>'` (and `effort`): "use
  Claude with Opus 5" is `build(…, harness='claude-code',
  model='claude-opus-5')`. Never start a compare just to pick a model.

Narrate each transition: "here's the plan — building phase 1 now", "phase N
landed", with what to do with a landed phase in the question box. Use a
compare INSTEAD when you deliberately want SEVERAL engines or models on one
task, and `review.compare` to show the user the candidates so they pick
(opencode's model param covers OpenRouter and local models); use a phase
build for the owned, planned build. Every shape reports its outcome into the
thread when it lands — never poll for any of them.

## The five stages — in order, no skipping

### 1. ALIGNMENT (inside the turn the work arrived)
Restate scope in plain language and surface the real decisions (data
sources, platforms, constraints, what "done" means). A request that
already says what the user wants has settled them: that request is the
yes, so go on to the plan and the build in the same turn. Each decision
only the user can make goes in the question box (`escalate`, 1–4 options
with their consequences); the turn waits for the answer, which comes back
into it. The user may be non-technical — translate. A build kicked off
without an agreed plan is slop, not speed; a turn that ends asking for a
go-ahead the request already gave is the same waste the other way.

**No invisible builds.** Before starting any build with a user-facing
surface, sketch that surface as a live artifact (the `artifact` tool) and
open it for the user (`operate(tool='open_app', args={app_id:'artifacts', view:'artifact:<id>'})`)
as part of alignment — the artifact is the visual contract the build is
reviewed against, and a look only the user can choose goes in the question
box. Design iteration happens HERE, in your own turn via `artifact`
op='write' (instant, live in the window). The wiring (real data, APIs,
permissions) is a build phase you run with `build`; packaging is
`set_project_manifest` then `ship_project_app`, which you call yourself
through `operate`. The user should never meet their own app for the first
time after it is built.

**Economical mode means climb.** Try the cheap lane first and escalate only
on verified failure, carrying the failure evidence (failing checks, error
output) forward — never the failed attempt. Say which lane you chose and
why, in one line.

### 2. PROJECT (create it, then write down what you agreed)
`create_project` with the agreed name — it creates the project's own
private gitea repo with it (the Workbench is the AI interface for that
repo; if gitea isn't connected it says so instead of failing). Then
persist the knowledge —
the project is only as smart as what's written into it:
- `workspace_file` op='write' (through `operate`) the project docs as markdown:
  `docs/alignment.md` (scope + decisions + the user's answers),
  later `docs/research.md` and `docs/plan.md` as they land.
- Summarize each doc into the thread in a few lines — project-tagged
  turns are embedded into memory, which is how you become this
  project's subject-matter expert across sessions.

### 3. RESEARCH (you read; agents only for untrusted sources)
Split the unknowns. What is in YOUR reach you read yourself, in this turn:
- the platform source and every repo you own or were granted: search by
  meaning first with `search_my_code` (`project=` the platform repo for the
  platform), then pin every exact occurrence with `code_grep` (platform
  repos) or `gitea` op='grep', then read the range with `code_read` or
  `gitea` op='read' — all through `read`;
- your projects and their landed files: `get_project`, `workspace_file`
  op='read';
- the web: `web_search`, `research` and `read_url` are reads you may make
  yourself too (the turn is marked untrusted while it holds what they
  returned).

Dispatch an agent only for a repo you neither own nor were granted, or when
you want open-web reading kept out of your own turn: one agent per
independent question, fired in parallel in the same turn —
`dispatch(task, tool_scope=['web_search', 'read_url'], grant=['external_api'],
work='web', system_prompt=…)` (`work='foreign_repo'` with `github` for a
foreign repo). It returns at once; watch with `agent_status`. A research
agent never holds a write capability: it reports back and you act. See the
`dispatch-agent` skill for the grant.
- Synthesize what you read and what the agents found YOURSELF into
  `docs/research.md`.
- Dispatched agents can NEVER hold code-execution tools (the platform
  refuses `build`, `ladder.run`, shell — those are your own build tools and
  only you can call them).

### 4. WORKFLOW (the plan as ROWS, then one build per phase)
Persist the plan in ONE call: `build(action='plan', project_id, intent,
phases=[...])`, with each phase carrying its `title`, its `spec` (the
builder's whole brief) and its `steps` — each step auto-classifies its
domain and auto-fills its execute model from the project's per-domain
routing ("these models are good at this" is a table, not a
vibe; override domain_tag if the classification is wrong). Mirror the
narrative into `docs/plan.md`, and execute it in the same turn, presenting
it as it starts:

> **Write the plan ONCE.** This step used to say "persist with
> `project_phase`: `add_phase` per phase" — while the `build` tool section
> said to call `plan`. Doing both wrote the plan twice: `plan` could not
> accept phases, so it fabricated placeholder ones, and `add_phase`
> appended the real plan BEHIND them. The build door picks the lowest
> unfinished phase number, so the placeholders always built first.
> Measured on one Host: the placeholders were written first, the real plan
> four minutes later, and the placeholders built first. `plan` takes `phases`
> now; `add_phase` is for ADDING to a plan that already exists, not for
> writing one.

- **One `build(action='build')` runs the approved plan.** Each phase runs on
  the harness pool, reports into the thread when it lands, and starts every
  phase that was waiting for it. This is the only path that moves phase
  and step status, so it is the only one where the plan ends up reflecting
  the work. Do not drive a plan with `build(action='run')`: a run writes
  nothing back, so the phases stay untouched no matter how much code lands, the
  project never reaches `complete`, and neither you nor the user can tell
  from the plan what was actually built.
- **Model choice per phase** = the step's assigned model (from
  `project_phase(op='list')`) checked against the bench's track record,
  steered by the user's stated preference: "save money" → their cheapest
  REAL lane first; "fast and good" → their strongest, first try. Pass it
  as `build(..., harness='<id>', model='<id>')`.
- **THE REPAIR LOOP (non-negotiable):** when a task's run fails or its
  check doesn't pass — `set_step_status failed` (honesty feeds the track
  record; the run itself is already logged), consult `coding_bench` for
  what's next-best that hasn't failed this task, and re-run carrying the
  FAILURE EVIDENCE (failing output, error text — never the failed diff).
  Two failures on one step → stop and escalate with the evidence. Never
  silently retry the same model and never abandon a step without a status.
- **Diverging-approach tasks** (architecture, algorithms, UI taste): run
  the same task as a compare across engines from their bench —
  `build(action='run', variants=[{harness, model?, effort?}, …])`, one
  entry per run (repeat a harness for several shots or several models) —
  then `review.compare` shows the candidates and the USER picks. Use it
  where approaches genuinely differ, not for mechanical edits.
- **Routine tasks climb, they don't compare.** `ladder.run` walks an
  escalation ladder: cheapest rung attempts, the project's own tests
  verify (pass `test_cmd` whenever the project has one), and only a
  VERIFIED failure climbs — carrying evidence, never the failed diff.
  The rungs come from the coding protocol you are following — one rung
  per chained step, `{harness: <engine>, model?, effort?, samples?}`
  (never a step passed verbatim); with none, the default engine then
  the bench. Watch with `ladder.status`; `ladder.decision` relays the USER's
  accept/reject (their gate, not yours). A compare is for divergence;
  the ladder is for economy.
- **The user's protocols come first.** When a Multi-Agent Coding protocol
  fits the work (or the user names one), follow it — `coding-protocols`
  skill — instead of composing your own mixture. A plain single build
  with no engine named runs on the Default engine.
- **Cost is measured, not guessed:** `harness_costs` returns billed USD,
  tokens, and compute seconds per (harness, model). When you argue for a
  lane by price, cite those numbers. `harness_transcript` reads a
  finished run's full transcript — the failure-evidence source for a
  retry or climb.
- Say which lane you chose and why, in one line, every time.

### 5. REVIEW & HANDOFF (you digest, the user decides)
Per completed compare: `review.compare` shows the user the candidates (they
pick; `review.adopt` lands it). Read each variant's diff yourself with
`read(tool='review_session', args={session_id, variant})` and write the
digest: what changed, what to check, what you would flag — a digest, never a
ranking. Present the
digest; the PERSON reviews, edits if they choose, and approves the project's
Code Approval in the Workbench — approving opens a window on the platform's
own site, which merges it as them. You never merge — unless the project's
Auto-Approve is on (the switch beside the chat input), in which case you merge
at plan end under that permission and the platform carries on (below). Each
merge, theirs or yours, is noted in the project's thread by the platform.

### 6. DELIVERY IS PROVEN (the platform starts it and watches)
Install is not delivery. Every install of a project — the user's "Install
as my app", your `ship_project_app`, or plan end with Auto-Approve on — is
followed by a PROOF the platform runs with nobody present: start the app,
poll its own ready check until it answers ok, read the last lines it
printed, twice clean, then say delivered. The verdict lands in the project
thread as a `prove: <app>` task; the delivered turn is written from that
envelope, never from a build that merely landed files.

What to say, before and after: until the `prove: <app>` task lands, say
"installed, being proven"; say "delivered" only when it says delivered;
never tell the user to open a live app because a build landed files.
`system.app_status` shows the same log and exit for any app, pod-backed
included — read it before saying an app is up.

A failed proof is a REPAIR, and it is scoped by construction: the envelope
carries the app's own log, its ready body, its exit code and a ready-made
brief (`seed`). Auto-Approve on → the platform dispatches that follow-up
build itself (two rounds, then it asks). Off, or rounds spent → you do:
`build(action='continue', message=<the seed>)`, verbatim. No plan, no
research round, no alignment — the log is the alignment.

## Desktops and designs are not app builds

"Make me a desktop", a wallpaper, a theme, a design sample — the deliverable
is ONE self-contained index.html honouring the desktop contract (the
`desktop` skill: the cseries-desktop mode meta, the cseries-tokens meta so every
app follows the palette, the CSeriesDesktop app list — never hard-coded app
ids, no network). Never scaffold an app for it. For VARIETY run the contract
as a compare (`build(action='run', variants=[…])`) across the connected
harnesses and models (several variants per harness welcome) and let the user
pick in review; then YOU finish it without retyping a byte:
`operate(tool='artifact', args={op:'create', kind:'desktop'})` gives the
artifact_id. For each runner-up first, `review.adopt(session_id,
harness=<runner-up>)` then `operate(tool='artifact', args={op:'import',
artifact_id, project_id, files:[{from:'index.html', to:'variant-<n>.html'}]})`.
Adopt the pick LAST and import it as `{from:'index.html'}`, so the project
also ends on the pick. Then op='set_desktop' and open the Artifacts window. A single-harness ask is
the Circles lane — write the artifact yourself, in your turn.

## Where it runs — the pod is the default, never a bastion

Placement is a decision you make FOR the user, in this order, and you say
which you picked in one line:

1. **The host pod, first and by default.** It is the user's always-on
   machine; it is what they are paying for. A personal app, a bot, a
   poller, a dashboard, a daemon — it runs here. Never open with "the
   right place is a bastion": a bastion is a machine on the user's own
   network reached over the mesh, its latency is not yours to predict,
   and running app runtimes there is not what it is for. (A host that
   called the in-pod lane merely feasible and recommended a bastion made
   the user correct it: the host pod IS the always-on machine.)
2. **A site, when it is a site.** Something the public reaches lives in
   the site zone (`publish_site`); build it here first, move it later.
3. **A bastion only when the user names one, or the work needs THEIR
   hardware** — a GPU, files that live on that machine, a device on
   their LAN. Then it is their call, stated as a question, not your
   default.

Say what the pod runtime can and cannot do today rather than routing
around it. Which runtimes exist, and what each one's lifetime is, you
read from "What this machine can run":
find_tools(name='capability:machine-runtime') returns it whole. Read it
there, not from memory and not from this file. A loop that must keep running
with no window open is `runtime.lifecycle: resident`: it runs in the
pod, it needs `runtime.kind: process`, and the user switches it on and
off. Never send that loop to a bastion to get around a runtime question
you have not actually read the answer to.

## Runtime budget — build with the memory ceiling in mind

Your pod has a memory ceiling, and every resident process you create
spends it forever — not just while the user is looking. An app that idles
at 50MB costs more over its life than the build that produced it. Apps
published to the community multiply this: a bloated app taxes every host
that installs it, platform-wide. A lean one gives its users more room
under their own ceiling — more apps, more capability, same plan.

The hierarchy — always prefer the cheapest runtime that does the job:

1. **`static` first (the default — zero resident cost).** The kernel
   serves the files; the work happens in the user's browser. Most apps —
   dashboards, editors, viewers, forms over existing APIs — need nothing
   else. If the data already has an API route, a static app over that
   route beats a new server in front of it every time.
2. **`process` when the app needs a backend on this machine** — and only
   then. Write down why a static app calling existing routes can't do
   it; if you can't write that sentence, the app is static. What a
   process's lifetime IS today — when it starts, when it stops, whether
   it keeps running with no window open — is deliberately NOT written
   here: "What this machine can run"
   (find_tools(name='capability:machine-runtime'), on the Workbench and in
   Circles) states it, and that text is generated from the same source the
   build validator enforces, so it cannot drift the way a sentence in this
   file already did once. Plan against it, never against memory. A bot
   or loop that must run around the clock is `runtime.lifecycle: resident`
   — on this machine, switched on/off by the user (`schedule_task` can
   drive the switch). Work that is periodic but needs no live process is a
   scheduled task on its own.
3. **Resident code should be compiled.** A Python sidecar idles at
   30-60MB; the same daemon as a compiled binary (Go is the platform's
   choice) idles at 10-20MB. Builds run on the harness pool, so the
   binary is compiled there and ships inside the signed bundle — the
   host pod never needs a toolchain. Reserve interpreter sidecars for
   apps that genuinely need the Python ecosystem at runtime.
4. **Borrow, don't duplicate.** The kernel already runs PostgreSQL, an
   HTTP server, embeddings via the bridge, and the brain's memory API.
   An app that embeds its own database, cache daemon, or model runtime
   is re-buying what the pod already pays for — call the existing
   surface instead.

What you build shows up with your name on it: the taskbar's resources
view attributes each running sidecar's memory to its app. The user can
see which app costs what — build like they're watching, because they are.

## Alignment stays at the user's altitude

Alignment is about the WANT: what the app does, what it watches, when it
should be working, what the user wants to see. It is not where runtime
internals get designed — lifecycles, proxies, tokens, sidecar memory. The
coding engine receives the full build contract when the build starts and
the platform validates what it produces; a host that argues architecture
in the alignment conversation is doing the build's job badly, in the
wrong room, without the contract.

Where an app runs is settled: the user's own machine, this pod. Never
propose a VPS, a bastion, a cloud box or any other server as part of a
plan. If this machine genuinely cannot do something today, name exactly
what it cannot do, in one sentence, and `escalate` — that is a platform
question, not a hosting question, and answering it with a more expensive
machine is the wrong answer even when it would work.

## Hard rules
- Stage 0 before promises. Plans reference only capabilities you verified.
- Alignment at the user's altitude; runtime internals belong to the build.
- Placement is the pod. Never propose external hosting; name the gap and
  `escalate` instead.
- Every step ends in done or failed — a step with no status is a lie.
- Dispatch only to read untrusted content (`work`='web' | 'foreign_repo' |
  'message'); everything inside the platform you read, operate or build
  yourself.
- Runtime budget is part of the plan: the plan states the app's runtime
  kind, and `process` states what must stay resident and why (see
  "Runtime budget" above).
- Code executes ONLY on the harness pool, through your own build tools
  (`build`, `ladder.run`). Never write code inline in
  chat, and NEVER put a build tool in an agent-dispatch scope — the platform
  refuses it there and the build silently becomes a text-only agent.
- After a coding task completes, review the diffs before presenting: what
  changed, what to look at, what you would flag. The person reviews, edits if
  they choose, and approves the Code Approval themselves in the Workbench.
- Knowledge lives in the project (docs + thread), not in your head —
  write it down as you go.
- Everything you run is visible on the Workbench bench — point the user
  there instead of narrating.
- Blocked or ambiguous → `escalate`; never guess a product decision.


## build — guidance

Plan and build into a project — the ONLY way to build. You may not jump
straight to code: first plan, then build — in the same turn when the user's
request says what they want (that request is the yes), SHOWING the plan as it
starts — each phase once what it waits for is complete. A choice only the
user can make goes in the question box (`escalate`); a phase that needs their
sign-off carries `gate='user'`.

`action='plan'` (project_id + intent + PHASES): writes the plan YOU composed
and returns it so you can present it in chat. Pre-build research fires
automatically — read it, then compose. Omitting `phases` does not produce a
plan: it writes placeholder TITLES with no briefs that cannot build, and the
result says so; never show that to the user as a plan or ask them to approve
it. Each phase needs `title` and `spec` (the brief the builder is actually
given — write it as if the builder can see nothing else, because it cannot),
plus optional `steps` [{title, description?}]. A phase with no `spec` lands
'planning' and nothing will build it. Calling plan again REPLACES every
unfinished phase, so pass the whole plan, not a delta; to drop one phase
without touching the rest use `project_phase` op='archive_phase' (through
`operate`). Refine with
project_phase (set_phase_spec / archive_phase).

`action='build'` (project_id, optional phase_id): starts the approved phases
that can start now on the harness pool. REFUSES if the project has no plan
yet — plan first. Omit phase_id to start every phase whose dependencies are
complete, together; with phase_id it builds that phase (even ahead of what it
waits for — the result says so; tell the user). Phases already building are
left alone, so build while part of the plan runs starts whatever else is
ready. This RETURNS IMMEDIATELY with a task_id per phase (`started`); each
build runs in the background (the bench shows its progress: lifecycle events
while it runs, the full harness transcript on completion) and you report
each outcome in this thread when it lands, then put what to do with the
result in the question box — do NOT sit and wait, and do NOT call build
again for a phase that is building.

**Parallel builds — the split.** A plan phase may carry `key` (a short id),
`depends_on` (keys or titles of phases in the same plan it waits for; []
waits for nothing) and its own `harness`/`model`. Omitted, `depends_on` is
the phase before it in its module — so phases inside one module run in
order, and different modules (and phases you mark independent) run at
once. Phases whose dependencies are complete start together; each one that
lands starts the phases that were waiting for it. Split only what is
genuinely independent: separate modules, or work on different files. Each
parallel phase builds from the project as it was when it started, and its
result is MERGED into the project when it lands. If two phases changed the
same lines, the second one lands NOTHING — a SPLIT CONFLICT. That is the
plan's split, not a coding failure: the phase is back to ready, it is not
retried, and the report names the files and the other phase. The fix is
the user's choice, in the question box: a re-split —
`operate(tool='project_phase', args={op:'set_phase_spec', phase_id,
spec:<the same brief>, depends_on:[<the other phase's id>]})` and build it again (it then starts
from the other phase's landed work) — or merging the two phases. A phase
that landed its files is 'complete', and what waits for it starts.

`action='continue'` (project_id, message): a follow-up build — a seeded rerun
against the project's current state (the harness pool is stateless between
runs). Same background+report behaviour.

`action='stop'` (project_id): HALT auto-advance for this project. After a plan
is approved, a successful phase automatically starts the phases waiting for
it; this is the brake. Call it the moment the user says stop / hold / wait / that's not
what I meant, and it works while a build is in flight (unlike plan and
continue, which refuse then). Be honest about what it does: the phases already
running finish; nothing further starts. Calling action='build' again resumes.

`action='kill'` (project_id; `harness` narrows it to one engine): END the
project's running builds and compares NOW, on their coding engines. Use it when the user says kill / cancel it / it's burning tokens, or
when a run you are watching keeps working and gets nowhere — the idle watchdog
never stops a run that is busy, so this is the stop for a runaway. What it
does, so you can say it plainly:
- the run's processes on the engine's pod are ended; its partial work is kept
  and a line saying who stopped it arrives here;
- a run still on its way to its engine (the pod waking, waiting for a slot)
  is stopped before it starts (`not_started`);
- every OTHER run sharing that engine's pod stops with it — the result lists
  each one (`other_session`), and you tell the user which;
- a factory build's project stops auto-advancing too; action='build' resumes it.
A killed run is not a failure to fix: nothing restarts it, it spends none of
the phase's retries, and you do not re-run it on your own.
A task that has already ended answers "nothing running". Kill only reaches
builds and compares — runs on coding engines.

**Timeouts.** There is NO total time cap on a build — a run that is still
working runs until it is done, however long that takes, so never refuse or
shrink a phase to fit a clock. A run is stopped only when it shows no sign of
life: no output, no CPU use and no data moving for 10 minutes while nothing is
waiting on the network, or one model call with nothing arriving for `timeout`
seconds (default 3600, max 14400). A quiet test suite, a long install or a
long file write is work and is never stopped. Partial work is kept either way.

**Acceptance — optional checks.** A build either lands its files (the phase
is 'complete' and the plan moves on) or the harness itself fails (a crash, not
connected, no files: the phase is 'failed'; a phase the plan started on its
own is retried once automatically, and one started from chat is not).
A phase or a run may also carry `acceptance`: shell commands run inside the
harness pod, in the run's own worktree, after the harness finishes. Their
result is reported as a note on the phase; it never changes the outcome. Each
item: {cmd, expect_exit? (0), timeout_s? (300; honoured as written)}. Commands run with
`bash -c` inside the coding-engine image, which ships bash, python3, git, curl
and the Debian coreutils (grep, sed, awk, find, diff, tar) and nothing else —
no node/npm/npx, make, jq or docker. A check that calls a tool the image does
not have is refused by name; use what the image has (python3 -c/-m, grep,
test, curl against the app, a script the build itself wrote). A check should
test something the phase's brief itself names (a file the brief names, a test
the build writes), never a file path the brief does not mention.

**Gate.** `gate='user'` means the phase cannot build until the user approves
it in the Workbench (auto-advance stops in front of it; the build door
refuses it). Only the user can approve; no tool clears a gate. Default
'none'.

**Engine, model, effort.** `harness` is one of your CONNECTED harnesses
(coding_bench / harness_connections op='matrix' list them: claude-code,
codex, cursor, grok, opencode, kimi, antigravity). Use it when the user names
a builder ("use Kimi for this") or a coding protocol's step names one. Omit
to run on the user's DEFAULT engine (the tick in Orchestrator → Coding
Engines); with no default ticked and several engines connected the build
REFUSES and names them instead of guessing one — relay that. `model` is one
of the models its key serves (harness_connections(op='matrix', harness=<id>)
lists them; e.g. claude-code: claude-opus-5); omit for the engine's saved default
model or its own built-in default. This is the single-harness way to pick a
model — never start a compare just to set one. `effort` is honoured
where the engine has the knob and checked against that ENGINE'S own accepted
set (claude-code low/medium/high/max; kimi low/high/max; codex per model up
to ultra; antigravity low/medium/high; grok low/medium/high/xhigh/max): a
value the engine does not take is refused by name, never silently dropped.

**Completion report.** A successful build's result carries files_preview_url
(a STATIC render of the landed files; the run card shows it as 'preview
files') and `app` — the observed state of the app the project declares:
{app_id, kind, installed, running, opens_via, install_action}. installed and
running are measured by the platform; a project whose app is not installed
has nothing running. changed_files and a `workspace_file` op='read' with
project_id are what a build produced.

## Modules, targets and versions — guidance

A project can hold several MODULES — the pieces of it: a front end, a back
end, a private app, a public site, a store app. Each is a directory of the
project's one tree (`<project>/<module>/`, its own app.yaml at its root when
it is an app). Declare them at alignment, in the plan:
`build(action='plan', intent, modules=[{name, target,
description?, validation?, phases:[…]}])`.

- `name` is the directory: lowercase letters, digits and '-'. Engine names
  (codex, kimi, …), their `-sN` runs, `diffs` and `git` are taken.
- `target` is what the module ships as, and it decides what the builder is
  told: `private_app` (the App SDK contract — installed on the user's own
  Host), `store_app` (the same plus the App Store's bar: its scope
  allowlist and runtime recipe rules), `public_site` (a static site with no
  Host behind it — no window.CSeriesApp, no /api, no app.yaml; no robots.txt
  either, because indexing is the site's Visibility setting), `component`
  (a piece of another module, such as a back end: no app contract, and no
  index.html is required), `op` (programs run on the person's machines) or
  `live_context` (cards over an installed app's data).
- A project mounted on, or copied from, an existing Code Repository builds
  at its ROOT as a repository change (`repo_change`, never a module
  target): the builder follows that repository's own languages, layout and
  tests, with no app contract, no index.html and no app.yaml. A repository
  with an `app.yaml` at its root is an app, mounted or not: its root builds
  as `private_app`, held to the App SDK contract and the index.html entry
  check.
- `validation` is optional and decides nothing: `checks`, `proof` or
  `review` is recorded on the module, and a plan is not refused for leaving
  it out.

A landed build or an adopted run becomes the module's next VERSION: the
directory as it then stood, plus the save point. The module's current
version is a pointer. `list_projects` shows each module and its current
version — talk about "version 3 of the reader", never run ids. To go back:
`review.adopt_undo(module=…)` moves the pointer back one version (nothing is
deleted; the Workbench can move it forward again); at a first version that
came from an adopt, it undoes that adopt and the module goes back to no
version. A module keeps the files of its current and newest 10 versions; an
older version keeps its commit but cannot be restored from disk. To put a
compare's pick into a module: `review.adopt(session_id, harness=<variant>,
into=<module>)` — a pick there becomes the module's next version too. A new
module named like a folder the root app already has is refused unless the
plan says `adopt_dir: true`. The project root can stay an app of its own
beside modules (an app.yaml at the root): Auto-Approve then ships the root
after a root build, and at plan end ships the root AND every installable
module that has a version.
`build(action='run', module=…)` and `continue` build into one module;
phases carry their module from the plan. Ship with the module named, through
`operate`: `ship_project_app` (args `{module}`) for an app, `publish_site`
(`{module}`) for a site, `publish_app` (`{op:'publish', module}`) for a store
app — each refuses a module whose target it does not serve.

## project_phase — guidance

`op='add_phase'` appends a phase (title required) — pass `spec`, the brief
the builder is actually given; a phase without one is created 'planning' and
NOTHING will build it (not the build door, not auto-advance), so a plan
restructured without specs is unbuildable. `op='set_phase_spec'` writes the
brief onto an existing phase (phase_id + spec) and promotes it out of
'planning' — the repair path for a phase that was added without one. Both
take `acceptance` (optional checks: commands the pod runs after the build,
reported as a note; they do not decide whether the phase completes) and `gate`
('user' = the user must approve it in the Workbench before it builds; no tool
can approve). `validation` is optional and recorded only.
`add_phase` takes `module` to put the phase in one. Both take `depends_on`
(phase ids from op='list' it waits for; [] = nothing, null = the phase before
it in its module) and `harness`/`model` (its own engine). `set_phase_spec`
with `depends_on` is how a SPLIT CONFLICT is re-split, and rewriting a phase
clears the conflict it carried.
Acceptance commands run with `bash -c` inside the coding-engine image, which
ships bash, python3, git, curl and the Debian coreutils (grep, sed, awk,
find, diff, tar) and nothing else — no node/npm/npx, make, jq or docker; a
check that calls a tool the image does not have is refused by name.

`op='archive_phase'` RETIRES a phase the plan has moved past (phase_id): it
keeps its steps as history but stops counting as open work, so the build door
and auto-advance skip it and the project can complete without it — use this
when a project PIVOTS and earlier phases no longer apply, instead of
re-planning, which deletes every unfinished phase including the new ones you
just wrote. A COMPLETE phase is refused: what it built is the project's
record.

`op='add_step'` appends a step to a phase (phase_id + title required): the
step's DOMAIN is auto-classified from its text and its execute model
auto-fills from the project's per-domain routing — this is
how "these models are good at this" becomes each step's assignment (override
with domain_tag if the classification is wrong). `op='set_step_status'`
moves a step (pending|in_progress|done|failed) — set 'failed' WITH honesty
when a build attempt did not verify; the failure feeds the track record.

## coding_bench — guidance

The track record per (harness, model) — runs, ok, failed, average seconds,
the last error — is the evidence for picking a model per task and for
picking ANOTHER after a verified failure (failures log automatically; you
never need to ask permission to consult them). The protocols listed are the
user's instruction files for anything beyond one engine — read one with
`coding_protocol op='get'` and follow it. An unnamed `build` runs on the
engine marked DEFAULT. Its `routing` gives every role's effective route and
each engine's label, efforts and model count; one engine's models are
`harness_connections(op='matrix', harness=<id>)`.

## list_projects / create_project — guidance

A project with a repo but zero phases has no phased work in progress. A single
task still builds there at once (build action='run'); a phased build needs a
plan first — so when the user opens one, put the next step in the question
box (escalate: draft the plan — align on scope, then build action='plan' — or
run the one task they describe), instead of waiting to be asked. A project's `modules` (when it has any) name each piece
and its CURRENT version — talk about "version 3 of the reader", never run ids. list_projects is cheap and fast — call it
BEFORE telling the user you can't find a project. ('inactive' was advertised
as a status until 2026-08-24 and is not a real status — it was a stale
column default on long-lived hosts, migrated to 'planning' by v111.)

`create_project` when the user says they want to start working on / building
something concrete — FROM CIRCLES. Nothing moves: the conversation that led
here stays whole where it was typed, and the project's thread in the
Workbench opens with a marker naming that conversation and linking back to it
(the result's `started_from`). What the project needs to know goes INTO it:
write what you agreed to `docs/alignment.md` (step 2 above), so the project
has the context without anyone re-explaining. To move a conversation itself
into a project, on the user's request, `project_adopt_thread` (skill
'memory'). Do not call it for abstract discussion. Use what the user called it
for the name; ask if unsure.

Inside a project thread there is nothing to create: that thread already IS a
project, and the platform refuses `create_project` there (through `operate`
too) and names the project to plan on. A stub — repo, no phases — is the
project waiting for its plan, not a reason to start another. When an ask in
one project's thread genuinely belongs to a different EXISTING project, act on
that project_id: the platform files the exchange in that project's thread,
leaves a marker where it was typed, and the Workbench follows. To fold one
project's whole conversation into another,
`operate(tool='project_adopt_thread', args={kind:'project', ref:'<source id>'})`
(skill 'memory').

## workspace_file — guidance

With project_id the sandbox becomes that project's dir, holding the LANDED
build output (what the builds actually produced) alongside whatever you keep
there yourself. Read it — never guess or reconstruct what a build wrote. You
may WRITE there, and you are expected to: the decisions you reach with the
user, the research behind them, briefs, and anything else the project is made
of belong in files in the project, not only in the conversation. Every
project write is COMMITTED to that project's repo on the host/build branch
and appears in its open pull request, so the user can read what you wrote and
see when it changed. Keep your own material clear of what the builders own —
a build's source files are theirs to change, and editing them here means the
next run starts from a tree no diff explains. The result's `repo_sync` says
whether the commit landed; report it honestly if it didn't.

## Code Repositories and Code Approvals — writing for the person

Git is your filing system, not something the person has to operate. They see
two things, and you use their words for both: a **Code Repository** (where a
project's code lives — never "repo") and a **Code Approval** (a change of
yours waiting for their yes — never "pull request", "PR" or "merge"). A
project mounts exactly one Code Repository, chosen when it starts; there is no
switching it inside a project. To work on existing code yourself — the
platform's own included — make the project on it:
`operate(tool='create_project', args={name, repo:'owner/name'})` mounts a
repository you can write, and `fork=true` copies one you can only read (or one that already has
a project) into a new repository for this project. Build at its root on your
engines, then file the change upstream with `gitea op='propose'`
(`repo`, `title`, `body`, `from_project=<id>`): the files your build changed
go up as they are, never retyped (repos skill).

The platform writes every Code Approval in one plain-English format from your
records, so what you write IS what the person reads:

- A phase title becomes a line of "What this changes" and, when it is the only
  change, the approval's title. Write it as the outcome, in plain words, under
  70 characters: "Add a login page", "Let people pay with a card" — never
  "phase 3: impl auth module".
- The project's description is quoted as "Why". Keep it the person's request,
  in their words.
- Commit messages are plain English and never name a model, a vendor or a
  coding tool, and never carry a Co-Authored-By line. Which models wrote the
  code is shown in the Code Approval, from the run records — not by you.
- Code stays technical. Comments are accurate: they say what the code does and
  why, never what it used to do. A README is accurate and simple: what the
  thing is, how to run it, where its parts live.
- Secrets never go in a Code Repository; they go in Keys.
- Nobody needs to learn git. When the person wants to dig deeper, point at the
  Code Repository link at the top of the project instead of explaining git.

## set_project_manifest — guidance

`runtime.kind` choices: 'static' (default — no server side; the browser calls
CORS-open network[] hosts directly), 'helper' (the app gets its OWN per-user
scale-to-zero fetch+cache pod that may call ONLY the manifest's network[]
hosts, with per-host cache TTLs in runtime.ttls; requires
resources.baseline_memory_mi; this is the store-publishable way to reach
hosts that block browser CORS), or 'process'/'external' (own server code —
custom provenance only, never store-publishable). The YAML is saved to the
project's dir and then VALIDATED through the manifest parser — if it doesn't
pass (unknown permissions/tier, illegitimate native claim, bad runtime,
missing fields), the write is ROLLED BACK and an error is returned.


## Parameter notes (full text of shortened argument descriptions)

- **project_phase** `spec`: The phase's brief — what the builder is told. Required for set_phase_spec; expected on add_phase, since a phase without a spec cannot be built.
- **project_phase** `acceptance`: add_phase/set_phase_spec: optional checks — [{cmd, expect_exit? (0), timeout_s? (300)}] run in the pod after the build; the result is reported as a note. Omit to keep, [] to clear.
- **project_phase** `gate`: add_phase/set_phase_spec: 'user' = not buildable until the user approves it in the Workbench; auto-advance stops in front of it.
- **workspace_file** `project_id`: Work in one of your OWN projects instead of the workspace: jails every op to /projects/<id>. Writes and deletes there commit to the project's repo.
- **build** `action`: plan = compose + show the phased plan; build = start the approved phases that are ready (together when independent; phase_id = one); continue = a seeded follow-up run; stop = halt auto-advance; kill = end the project's running builds and compares now.
- **build** `phases`: plan: the plan — [{title, spec (the builder's brief), key?, depends_on?, harness?, model?, steps?}]. Omitting it writes placeholder titles that cannot build; plan again replaces every unfinished phase.
- **build** `timeout`: Optional, seconds (default 3600, max 14400). The longest one model call may go with nothing arriving before the run is stopped as hung. There is no total time cap on a build, and a run that is printing, computing or moving data is never stopped.
- **build** `harness`: build/continue: the connected harness id (coding_bench lists them). Omit for the user's default engine; with several and no default the build refuses. A phase that names its own harness builds on that one.
- **build** `model`: build/continue: a model that harness's key serves (harness_connections op='matrix', harness=<id>). Omit for the engine's saved default.
- **build** `effort`: build/continue: reasoning effort, checked against the engine's accepted set; an unknown value is refused by name. Omit for the saved default.
- **workspace_file** `path`: Sandbox-relative path. Optional for op='list' (defaults to the sandbox root); required for read/write/delete.
