# Privatae C-Series — Architecture Reference

**Version:** pre-release. Releases take their number from the `vX.Y.Z` tag (`docs/RELEASING.md`); the tree itself carries no release number.
**Last updated:** 2026-10-20
**Status:** Pre-release. It builds and runs from source and as two containers; the `npx cseries` installer is unpublished. What is still to do is listed under Status in `README.md`.

---

## What This Is

Privatae C-Series is the software of a Host: an open-source (AGPL-3.0-only) AI agent that runs on
the person's own machine as two containers, the Host and its PostgreSQL database, using models the
person brings (their own provider keys, or a local model server). Nothing in it is hosted by
Privatae. The bastion, a separate GPL program on the same machine, decides what the Host may touch
there. In this file "the platform" means the kernel inside the Host, not a hosted service. The kernel owns four things: the walls (capability gate, turn types, the root
turn, approvals, the `host_world` role), the transcript, the person's standing instructions (their
preferences and rules, their personality sliders and explanation level, the platform's five
values) and the facts it measures about each turn. The Host owns everything else in its own
schema, `world`: the tables, views and functions it designs to hold what it learns, through the
`data` tool. No code models the model: there is no simulated mood, no personality drift, no
keyword scoring of the reply and no second model deciding what to remember.

## Core Thesis

The model uses its full capability and the person keeps control. Control lives in code (walls
the model cannot talk its way past); capability lives in the model and in what it builds in
`world`. `docs/HOST-MEMORY-AND-PROMPT.md` describes the memory and the prompt as they are.

---

## Directory Structure

```
/app/
├── brain/mastercode/           # The Host's runtime
│   ├── runtime/
│   │   ├── session.py          # Core EntityRuntime: the turn
│   │   ├── prompt_schema.py    # The system prompt's blocks, in order
│   │   ├── prompt_builder.py   # Block builders (identity, tendencies, discipline)
│   │   ├── crisis.py           # The crisis floor
│   │   ├── close_turn.py       # The reply's closing note: preferences, goal, handoff
│   │   ├── turn_types.py       # Turn types
│   │   ├── root_turn.py        # The root turn's allowlisted context
│   │   ├── model_style.py      # Per-model calibration block
│   │   ├── triage.py           # Message classification
│   │   ├── tools.py            # Tool execution
│   │   └── context.py          # Context management
│   ├── constitution/           # The platform's five values; the per-turn rules block
│   ├── world_db.py             # The `data` tool: SQL in `world` as host_world
│   ├── memory_engine.py        # Turn rows, the memory block, taught rules
│   ├── memory_consolidation.py # The insert helper typed writers share
│   ├── memory_retrieval.py     # recall across memory, documents, projects, code, skills, world
│   ├── thread_consolidator.py  # The rolling summary of a Circles conversation
│   ├── thread_audit.py         # The thread audit's findings, read by the memory block
│   ├── knowledge_toc.py        # What a thread holds, as counts
│   ├── codebook.py             # The registry row and permission grants
│   ├── provider_gateway.py     # Multi-provider inference routing
│   ├── domain_classifier.py    # Knowledge domain tagging (20 domains)
│   └── brain/                  # Scheduled/offline lanes (task runner, clock)
├── brain/ceigas_kill_switch.py # The kill switch
├── server/                     # FastAPI dashboard + API
│   ├── routes/
│   │   ├── brain.py            # Chat, memory, personality settings
│   │   ├── projects.py         # Projects V2 execution engine
│   │   ├── inference.py        # Model catalog + role assignment
│   │   └── ...
│   ├── middleware/
│   │   ├── localhost_only.py   # loopback or the owner's session
│   │   └── ceigas_gate.py      # CEIGAS authorization on every /api/* call
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
├── vm/wsl/                     # The Linux virtual machine image for Windows
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
  in its own turn: the reply's closing note (`runtime/close_turn.py`), the `memory_curate` tool,
  a "Remember this:" message, a promoted research finding. A shaped fact lives in a `world` table.
- The ten personality sliders are the person's and never move on their own. They render as
  tendency lines, one graded line per slider (`runtime/prompt_builder.py:26-40`).
- The person's rules reach a turn by meaning when the turn has an embedding and by importance
  when it does not (`constitution/context.py`), and only where they ride (`memory_scope`).
- The crisis floor (`runtime/crisis.py`) never gates the model: the person gets the platform's
  notice and the model is told the message was flagged.

---

## Projects V2 — Execution Engine

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

1. **Local**: a model server on the person's machine, reached through the bastion.
2. **Direct**: the person's own key, sealed in the Keys app: OpenRouter, Anthropic, OpenAI, xAI,
   Google, Kimi, DeepInfra, Together, Fireworks or Groq. The call goes from the Host to the
   provider.

A role may carry a fallback provider and model, used once when the first call errors; the result
says it failed over.

### Roles

`chat`, `review`, `embedding`, `image`, `image_gen` (`server/routes/inference.py:66-67`). There is no memory role.

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
Its connection to the network door is outbound only and carries only the App Store, community board,
passport and heartbeat requests (`server/bridge/member_client.py`, `NETWORK_OPS`).

---

## Known Issues / Technical Debt

### Critical

- **session.py is over 11,000 lines.** Needs decomposition into: intake/triage, context build, model call, post-message hooks. Target: no file > 800 lines.

### High

- Apps and engines that run in their own container (the connectors, the browser, the coding agents, the creative engines) still start through the platform bridge (`harness.wake`), which a standalone Host does not have. The local runtime that replaces it is not built yet.
- Backups are written and uploaded through platform operations (`server/host_backup.py`), which a standalone Host does not have. A standalone Host cannot yet write or restore its own backups.
- Code Repositories and Code Approvals need a Gitea forge (`GITEA_URL`), and the Host's installer configures none.
- Some app text still describes the hosted service the code was written for (for example the Settings backup and update tabs, and the Compute app's Billables view).
- ~~**Per-tool governance is stubbed** in code_exec.py (marked "Phase 8.5").~~ **Resolved
  — and `code_exec.py` no longer exists.** Per-tool governance is live and default-deny:
  `brain/mastercode/code_exec_guard.py` (`_is_code_exec_tool` + the harness /
  dispatched-agent boundary), `brain/mastercode/runtime/tool_access.py`, and
  `brain/mastercode/runtime/scoped_agent.py` (which denies code-exec tools to scoped
  agents). "Phase 8.5" appears nowhere in the code.

---

## Entity Identity

- A standalone Host is entity 1 (`brain_service._entity_id`, `server/brain_service.py:150`); the schema reserves id 0 for a platform's own row.
- A Host that joined the network has a second id, the network's, kept in its passport and shown in Settings. It is never sent in a request.

---

## Security Model

- **Localhost-only middleware** (`server/middleware/localhost_only.py`): a request must come from 127.0.0.1 or ::1, or carry the owner's signed-in session; `/mcp` has its own bearer and hardware-fingerprint authorization
- **CEIGAS gate** (`server/middleware/ceigas_gate.py`): every /api/* request authorized
- **Published on loopback only**: the dashboard (8800) and the bastion's two ports (8810, 8811) are bound to 127.0.0.1 in `compose.yaml`
- **Read-only filesystem**: both containers run with `read_only: true`
- **No root**: UID 1000
- **Seccomp** (`seccomp-personal.json`): the Host's profile refuses memfd_create, ptrace, unshare, mount and others
- **Cap drop ALL**: no Linux capabilities, and `no-new-privileges`
- **The database has no network port**: `listen_addresses = ''`, a Unix socket only
- **Symlink check**: workspace/artifact paths are resolved and symlink-escapes refused
  in `server/routes/codespace.py`, `server/routes/review.py`, and
  `server/applications/sdk_store.py`
- **Security headers** (`server/main.py`): X-Content-Type-Options, X-XSS-Protection, Referrer-Policy, X-Frame-Options: SAMEORIGIN
- **Entity_id validation**: numeric check in entrypoint prevents SQL injection
