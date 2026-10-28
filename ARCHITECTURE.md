# Privatae C-Series — Architecture Reference

Releases take their number from the `vX.Y.Z` tag (`docs/RELEASING.md`); the tree itself carries no
release number. Each release publishes the images and the `cseries` npm package that installs them.

---

## What This Is

Privatae C-Series is the software of a Host: an open-source (AGPL-3.0-only) AI agent that runs on
the person's own machine, in a Linux virtual machine, using models the person brings (their own
provider keys, or a local model server). The Host and its PostgreSQL database are two containers;
apps, the browser and the coding engines run in containers the local runtime (`vm/runtime/`)
starts beside them. The bastion, a separate GPL program on the same machine, runs as a system
service paired with the Host. In this file "the platform" means the kernel inside the Host, not a hosted service. The kernel owns four things: the walls (capability gate, turn types, the root
turn, approvals, the `host_world` role), the transcript, the person's standing instructions (their
preferences and rules, their personality sliders and explanation level, the platform's five
values) and the facts it measures about each turn. The Host keeps what it learns as notes it
writes in its own turns, writes its own skills, and keeps its own app data: structured data it
maintains without building an app, in tables, views and functions it designs in its own schema,
`world`, through the `data` tool.

## Core Thesis

The model uses its full capability and the person keeps control. Control lives in code (walls
the model cannot talk its way past); capability lives in the model, in what it remembers, in the
skills it writes and in what it builds in `world`. `docs/HOST-MEMORY-AND-PROMPT.md` describes the memory and the prompt as they are.

---

## Directory Structure

```
/app/
├── brain/mastercode/           # The Host's runtime
│   ├── runtime/
│   │   ├── session.py          # Core EntityRuntime: the turn (_send_message_core) and its tool loop (_run_tool_loop)
│   │   ├── turn_input.py       # Words sent into a running turn, in order
│   │   ├── turn_holds.py       # What keeps a turn open and wakes one; backstops
│   │   ├── turn_trace.py       # Reply pieces and the live turn trace
│   │   ├── turn_replies.py     # The turn's reply recorder (TurnReplies), read by the go-ahead guard
│   │   ├── turn_state.py       # What the tool loop shares with the rest of the turn (TurnState)
│   │   ├── turn_lanes.py       # A turn's lane and what it may do
│   │   ├── orchestration_tools.py # The Host's verbs and the orchestrator prompt
│   │   ├── prompt_schema.py    # The system prompt's blocks, in order
│   │   ├── prompt_builder.py   # Block builders (identity, tendencies, discipline)
│   │   ├── crisis.py           # The crisis floor
│   │   ├── close_turn.py       # The reply's closing note: preferences, goal, handoff
│   │   ├── turn_types.py       # Turn types
│   │   ├── root_turn.py        # The root turn's allowlisted context
│   │   ├── model_style.py      # Per-model calibration block
│   │   ├── tools.py            # Tool execution
│   │   ├── user_limits.py      # The person's limits and their defaults
│   │   ├── scheduled_approval.py # The person's approval for recurring model work
│   │   └── engine_research.py  # Web search on a connected coding engine
│   ├── constitution/           # The platform's five values; the per-turn rules block
│   ├── world_db.py             # The `data` tool: SQL in `world` as host_world
│   ├── memory_engine.py        # Turn rows, the memory block, taught rules
│   ├── memory_retrieval.py     # Recall
│   ├── thread_audit.py         # The thread audit's findings
│   ├── codebook.py             # The entity row and permission grants
│   ├── provider_gateway.py     # Multi-provider inference routing
│   ├── domain_classifier.py    # Knowledge domain tagging (20 domains)
│   └── brain/                  # Scheduled/offline lanes (task runner, clock)
├── brain/ceigas_kill_switch.py # The kill switch
├── server/                     # FastAPI dashboard + API
│   ├── routes/
│   │   ├── brain.py            # Chat, memory, personality settings
│   │   ├── projects.py         # Projects, phases and steps
│   │   ├── inference.py        # Model catalog + role assignment
│   │   ├── limits.py           # The Settings app's Limits page
│   │   └── ...
│   ├── applications/
│   │   └── local_runtime.py    # The Host's client for the local runtime
│   ├── middleware/
│   │   ├── localhost_only.py   # loopback or the owner's session
│   │   └── ceigas_gate.py      # The kill switch on mapped /api routes
│   ├── capabilities/
│   │   └── file_access.py      # host file-access capability (linux/ + windows/ backends)
│   ├── network/                # Bastion pairing and lanes, the egress gate, signed network rules
│   │   └── local_bastion.py    # the doors a bastion on the same machine uses
│   └── bridge/
│       ├── no_platform.py      # stands in for a platform on a standalone Host
│       ├── bridge_client.py    # mTLS JSON-line client (base of the member client)
│       ├── member_client.py    # network member: outbound-only mTLS to net.privatae.ai:8643
│       ├── membership.py       # join / renew / leave the network with a one-time code
│       └── passport.py         # the passport files under /var/lib/cseries/passport
├── system-apps/                # Dashboard apps (HTML/JS iframes)
├── default-skills/             # The skills a Host reads on demand
├── dashboard/                  # Desktop shell (base + custom overlay)
├── etc/
│   └── ceigas_root_v2.crt      # the network's root certificate, the trust anchor for membership
├── packages/cseries/           # The `npx cseries` installer
├── sidecars/                   # Container images for coding engines, the browser and app runtimes
├── vm/wsl/                     # The Linux guest: WSL's root filesystem and the guest's own files
├── vm/lima/                    # The guest disk image for macOS and Linux (Lima)
├── vm/runtime/                 # The local runtime (cseries-runtime) in the guest
├── vm/cloud/                   # A Host on a VPS or bare-metal machine with its own front door
├── init-db.sql                 # Schema; migrations/ carries every change since
├── migrations/
├── entrypoint.sh               # Boot: identity path → PG → migrations (`entrypoint.sh:453`) → FastAPI
├── Dockerfile.linux            # Multi-stage build
├── LICENSE                     # GNU AGPL v3 (AGPL-3.0-only)
└── ARCHITECTURE.md             # This file
```

---

## Memory and the Prompt

`docs/HOST-MEMORY-AND-PROMPT.md` lists every writer and reader of `entity_memory`, the memory
types `migrations/v235_one_memory.sql` keeps, and the prompt's blocks in render order. In short:

- Every turn is a row, written as it happens. What the Host keeps beyond the transcript it writes
  in its own turn, as notes in `entity_memory`: the person's preferences and the conversation's
  goal from the reply's closing note (`runtime/close_turn.py`), preferences and notes about people
  through the `memory_curate` tool, rules the person teaches ("Remember this:"), facts promoted
  from a task's findings, and research. No background pass reads conversations afterwards.
- A note can carry a topic. A write to a topic that already has a live note retires the older one
  in the same transaction and keeps it on record, so the newest note is the live one
  (`memory_slots.py`, `migrations/v241_memory_slots.sql`).
- Matching preferences and notes about people ride each turn (`memory_engine.py`,
  `PROMPT_MEMORY_TYPES`). The `recall` tool searches notes, documents, projects, code, skills and
  `world` together, each store answering under its own section (`memory_retrieval.py`,
  `SEARCH_STORES`). What the person tells the Host about themselves is a preference; a `world`
  table holds data the Host builds for a task.
- The Host writes its own skills (`skills/<name>` through `runtime/host_skills.py`). Each write
  is checked against `runtime/skill_standard.py` before it is saved and becomes a new version; the
  person can restore any version in the Skills app. A skill written in a turn that read untrusted
  content is marked untrusted until the person saves it. The daily review the person can ask for
  (`default-skills/housekeeping.md`) is one recurring scheduled task, set only after they approve
  it.
- The ten personality sliders are the person's and never move on their own. They render as
  tendency lines, one graded line per slider (`runtime/prompt_builder.py:26-40`).
- The person's rules reach a turn by meaning when the turn has an embedding and by importance
  when it does not (`constitution/context.py`), and only where they ride (`memory_scope`).
- The crisis floor (`runtime/crisis.py`) never gates the model: the person gets the platform's
  notice and the model is told the message was flagged.

---

## Projects

### Schema

```
projects → project_phases → phase_steps → step_provenance
                                        ↘ project_routing
```

Each step has: `domain_tag`, `spec_model`, `execute_model`, `check_mode`, `check_model`, `model_override`. The domain classifier auto-tags steps.

### How a step runs

There is no step-execute route: the step routes update a step and record its provenance
(`server/routes/projects.py`, `PUT /{project_id}/steps/{step_id}` and `.../provenance`). Coding
work runs through the harness pool (the `build` tool and `review.fan_out`).

---

## Inference Routing

### Two Provider Paths

Every call runs on the person's own provider account (`brain/mastercode/provider_gateway.py`).
There is no Privatae inference lane, and Privatae does not sell, proxy or bill model usage.

1. **Local**: a model server on a machine of the person's that a paired bastion reports.
2. **Direct**: the person's own key, sealed in the Keys app: OpenRouter, Anthropic, OpenAI, xAI,
   Google, Kimi, DeepInfra, Together, Fireworks or Groq. The call goes from the Host to the
   provider.

A role may carry a fallback provider and model, used once when the first call errors; the result
says it failed over.

### Roles

`chat`, `embedding`, `image`, `image_gen` (`server/routes/inference.py`, `ROLES`). `chat` is the
one text role. When no routed model can search the web, the Host's own web search runs on a
connected coding engine that can (Antigravity or Claude Code), and says which one
(`runtime/web_research.py`, `runtime/engine_research.py`).

---

## First Boot

1. The database container starts first and owns the data directory; the Host container reaches it
   over a shared Unix socket and never mounts the data directory.
2. `entrypoint.sh` decides the identity path (below), waits for the database, loads the schema and
   applies `migrations/v*.sql` once each, recorded in `schema_migrations`.
3. A new Host writes its own first-run identity and tool permissions (`server/local_identity.py`,
   `server/bridge/identity_seed.py`). A turn that arrives before that finishes waits up to ten seconds
   (`brain/mastercode/runtime/identity_hold.py`).
4. The Host prints a setup code. The owner opens `/setup`, enters the code, and chooses an email and a
   password of at least 12 characters (`server/owner_login.py`).

### Three Identity Paths (entrypoint.sh)

1. The persistent volume holds certificates from an earlier boot with a platform: restore them, boot.
2. `RELAY_CONFIG`, `RELAY_CERT`, `RELAY_KEY` and `CA_CERT` are set (a managed deployment): decode,
   persist, boot.
3. Neither: a standalone Host. It takes the row `init-db.sql` creates (id 1), has no platform, and
   gets a `NoPlatform` client (`server/bridge/no_platform.py`). This is the path the installer's
   containers take.

A standalone Host can join the Privatae network with a one-time code (`server/bridge/membership.py`).
Its connection to the network door is outbound only and carries only the operations in `NETWORK_OPS`
(`server/bridge/member_client.py`): App Store, community board, passport, heartbeat, rendezvous and
backup storage requests.

---

## Known Issues / Technical Debt

- `runtime/session.py` is about 9,450 lines, most of it two methods of `EntityRuntime`: the turn,
  `_send_message_core` (about 3,450 lines), and its tool loop, `_run_tool_loop` (about 2,450). Its
  module-level helpers live in turn_input, turn_holds, turn_trace, turn_notices,
  orchestration_tools, turn_lanes and delivery_checks, and session.py re-exports every name. What
  the loop shares with the rest of the turn is on one `TurnState` (turn_state.py), which the core
  makes and hands to the loop.
- The desktop and the system apps under `/system-apps/` are served with no Content-Security-Policy
  header: `security_headers` in `server/main.py` sets one only on an app's own origin, and
  `server/applications/proxy.py` adds one only to what a community app serves.

---

## Entity Identity

- A standalone Host is entity 1 (`brain_service._entity_id`, `server/brain_service.py:150`).
- A Host that joined the network also keeps the network's id for it in its passport (`server/bridge/passport.py`).

---

## Security Model

Each claim C-Series makes about its security is listed with the test that enforces it in
[docs/ai/claims.md](docs/ai/claims.md); [docs/ai/threat-model.md](docs/ai/threat-model.md) says what a
Host defends against and what you still have to trust. Report a vulnerability as
[SECURITY.md](SECURITY.md) says.

- **Localhost-only middleware** (`server/middleware/localhost_only.py`): a request must come from 127.0.0.1 or ::1, or carry the owner's signed-in session; the sign-in and setup pages, and a few paths that check their own credential, are the exceptions
- **CEIGAS gate** (`server/middleware/ceigas_gate.py`): the kill switch. While it is set, requests under the mapped `/api` prefixes are refused unless they are a stop or a governance call. What a caller may do is checked per route (`requires_permission`) and, for the Host's own tools, by the capability gate
- **Published on loopback only**: the dashboard (8800) and the bastion's two ports (8810, 8811) are bound to 127.0.0.1 in `compose.yaml` and in the guest's `cseries-host.container`
- **The machine wall** (`vm/wsl/rootfs/etc/cseries-machine/machine.nft`, in the WSL and Lima guests): no container in the virtual machine opens a connection to the machine under it (its loopback as the VM reaches it, the guest's gateway, the machine's own addresses); `npx cseries` writes it into every guest, an older one included (`packages/cseries/src/vm/machine-firewall.js`)
- **Read-only filesystem**: both containers run read-only
- **No root**: UID 1000
- **Seccomp** (`seccomp-personal.json`): the Host's profile refuses memfd_create, ptrace, unshare, mount and others
- **Cap drop ALL**: no Linux capabilities, and `no-new-privileges`
- **The database has no network port**: `listen_addresses = ''`, a Unix socket only
- **Path checks**: a build's files are resolved and kept inside their project (`server/routes/review.py`);
  the code browser lists a symlink without following it (`server/routes/codespace.py`)
- **Security headers** (`server/main.py`): X-Content-Type-Options, Referrer-Policy, X-Frame-Options: SAMEORIGIN (an app origin gets a CSP frame-ancestors instead). The desktop shell's own page also carries a Content-Security-Policy (`server/shell_csp.py`): it loads, connects and frames only its own origin and, on each of the Host's desktop names (the CSERIES_URL address and the remote address), that name's app zone on the same scheme and port, so script injected there reaches no other host from the shell's own document. It still allows inline script, which the Host-made desktop needs while it is a srcdoc frame, and it covers that one document only: script in the shell can still reach another host through a same-origin document that carries no policy of its own (an API answer, a stylesheet, a same-origin app's page), framed or opened (measured and pinned in `scripts/test_shell_csp_chromium.py`)
- **Entity id from a relay identity**: on the relay identity path, where the id is read from `relay.json`, `entrypoint.sh` refuses an id that is not a number. A standalone Host does not take that path; it is entity 1
