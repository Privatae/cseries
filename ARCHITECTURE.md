# Privatae C-Series — Architecture Reference

**Version:** 0.7.0-dev
**Last updated:** 2026-10-17
**Status:** Pre-production. Provisioning working. UX incomplete.

---

## What This Is

Privatae C-Series is the software of a Host: a container with its own database, running a model
the person chooses. The kernel owns four things: the walls (capability gate, turn types, the root
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
│   ├── domain_classifier.py    # Knowledge domain tagging (13 domains)
│   └── brain/                  # Scheduled/offline lanes (task runner, clock)
├── brain/ceigas_kill_switch.py # The operator kill switch
├── server/                     # FastAPI dashboard + API
│   ├── routes/
│   │   ├── brain.py            # Chat, memory, personality settings
│   │   ├── projects.py         # Projects V2 execution engine
│   │   ├── inference.py        # Model catalog + role assignment
│   │   └── ...
│   ├── middleware/
│   │   ├── localhost_only.py   # 127.0.0.1 + Docker bridge only
│   │   └── ceigas_gate.py      # CEIGAS authorization on every /api/* call
│   ├── capabilities/
│   │   └── file_access.py      # host file-access capability (linux/ + windows/ backends)
│   └── bridge/
│       ├── no_platform.py      # stands in for the platform on a standalone Host
│       ├── bridge_client.py    # mTLS JSON-line client (cloud bridge; base of the member client)
│       ├── member_client.py    # network member: outbound-only mTLS to net.privatae.ai:8643
│       ├── membership.py       # join / renew / leave the network with a one-time code
│       └── passport.py         # the passport files under /var/lib/cseries/passport
├── system-apps/                # Dashboard apps (HTML/JS iframes)
├── default-skills/             # The skills a Host reads on demand
├── dashboard/                  # Desktop shell (base + custom overlay)
├── provisioning/
│   └── ceigas_ca.crt           # CEIGAS CA cert
├── init-db.sql                 # Schema; migrations/ carries every change since
├── migrations/
├── entrypoint.sh               # Boot: certs → PG → migrations (`entrypoint.sh:453`) → FastAPI
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

### Three Provider Paths

1. **Local** — Ollama on user's machine (free). Qwen3-14B for chat + code.
2. **Privatae** — Platform gateway. DeepInfra/Fireworks backend. Billed per token.
3. **Direct** — User's own API keys. OpenRouter, Anthropic, xAI.

### Roles

`chat`, `review`, `embedding`, `image`, `image_gen` (`server/routes/inference.py:66-67`). There is no memory role.

---

## Provisioning

### Self-Provisioning (no FDA, no env vars)

1. Container boots, finds no certs in persistent volume
2. Connects to CEIGAS bridge via mTLS using bootstrap cert (baked in image)
3. Bridge creates entity in platform DB, generates keypair, signs cert with CEIGAS CA
4. Returns: entity_id + cert + key + CA cert + relay config
5. Container persists to volume, boots normally
6. Subsequent boots restore from volume

### Three Boot Paths (entrypoint.sh)

1. Persistent volume has certs → restore → boot
2. Env vars RELAY_CONFIG/RELAY_CERT/RELAY_KEY/CA_CERT → decode → persist → boot (enterprise)
3. Nothing → self-provision via mTLS bootstrap → boot

---

## Known Issues / Technical Debt

### Critical

- **session.py is over 11,000 lines.** Needs decomposition into: intake/triage, context build, model call, post-message hooks. Target: no file > 800 lines.

### High

- **`_template` app ships to users.** Should be excluded from production image.
- **Support modal uses inline styles and hex colors.** Violates Threshold design system.
- **Relay client leaks connections.** Entity relay reconnects every 2s when cert is rejected, tunnel never closes old connections. FD exhaustion within minutes.
- ~~**Per-tool governance is stubbed** in code_exec.py (marked "Phase 8.5").~~ **Resolved
  — and `code_exec.py` no longer exists.** Per-tool governance is live and default-deny:
  `brain/mastercode/code_exec_guard.py` (`_is_code_exec_tool` + the harness /
  dispatched-agent boundary), `brain/mastercode/runtime/tool_access.py`, and
  `brain/mastercode/runtime/scoped_agent.py` (which denies code-exec tools to scoped
  agents). "Phase 8.5" appears nowhere in the code.

### Medium

- **Memory context uses character cap (2000) not token cap.**
- **No WCAG 2.1 AA compliance.** Missing ARIA roles, keyboard navigation, focus management.
- **12 apps overwhelm beginners.** Should show 3 by default (Cortex, Projects, Files), rest behind "More."

---

## Entity Identity

- Entity PK in container DB matches `privatae_entity_id` from relay config
- Entrypoint.sh sets `id = $ENTITY_ID` on the seed row at first boot
- Teaching memories load with correct entity_id via sed replacement
- `brain_service._entity_id` resolves from relay config during FastAPI lifespan init

---

## Security Model

- **Localhost-only middleware** — 127.0.0.1, ::1, Docker bridge 172.17-18.x
- **CEIGAS gate** — every /api/* request authorized
- **Read-only filesystem** — container runs with `read_only: true`
- **No root** — runs as UID 1000 (privatae user)
- **Seccomp** — blocks memfd_create, ptrace, unshare, mount
- **Cap drop ALL** — no Linux capabilities
- **Symlink check** — workspace/artifact paths are resolved and symlink-escapes refused
  in `server/routes/codespace.py`, `server/routes/review.py`, and
  `server/applications/sdk_store.py`
- **Security headers** — X-Content-Type-Options, X-XSS-Protection, Referrer-Policy, X-Frame-Options: SAMEORIGIN
- **Entity_id validation** — numeric check in entrypoint prevents SQL injection
