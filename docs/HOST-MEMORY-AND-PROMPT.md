# The Host's memory and prompt

What the platform keeps for a Host, what the Host keeps for itself, who writes and reads
`entity_memory`, and the blocks of the system prompt. Code is cited as `file::function`; the
code is the reference when this page and it disagree.

> In this document "the platform" means the C-Series kernel inside the Host: the code outside
> the Host's own `world` schema. It does not mean a hosted service.

## 1. What the platform owns and what the Host owns

The platform owns:

- **The walls:** the capability gate, turn types, approvals, the `host_world` role and the
  operator kill switch.
- **The transcript:** every turn, and the markers a thread operation leaves (pick, split,
  rehome, started-from, adopt).
- **The person's standing instructions:** their preferences and rules, their personality sliders
  and explanation level (`runtime/prompt_builder.py`), and the platform's five values
  (`brain/mastercode/constitution/platform_values.py`).
- **The facts it measures about this turn:** frame, timeline, app state, diagnostics, live cards.

The Host owns the schema `world`. It creates the tables, views and functions a request needs
there through the `data` tool, which hands every op to `brain/mastercode/world_db.py::run_op`.
The SQL runs as the role `host_world` (`migrations/v233_host_world.sql`), so Postgres enforces
the wall whatever SQL the model writes: the role owns `world`, reads only the kernel tables on
v233's READABLE list, and writes nothing outside `world`. The kernel requires a COMMENT on every
object in `world` saying what it is for. Before a call that could remove or reshape data,
`world_db` takes a `pg_dump` of `world` as `host_world`. `migrations/v234_world_recall.sql` adds
`world_catalog_vectors` (so recall can search `world` by meaning) and `memory_recall_aliases`
(words the Host adds to a memory with `memory_curate op='alias'`). The `world` skill
(`default-skills/world.md`) is where the Host reads how to use it.

What the person tells the Host about themselves (the tools in their kitchen, a habit they are
building) is memory: a preference, kept in the turn with `close_turn`'s preferences or
`memory_curate`, where the newest entry in a topic wins. A `world` table holds data the Host builds
for a task, such as a meal plan or a tracker.

### The kill switch

While the file `ceigas_kill_switch` exists in the kill-switch directory
(`brain/ceigas_kill_switch.py`), every crossing that does not touch `core.governance` is
refused, except the crossings in `KILL_SWITCH_STOP_CROSSINGS`, which only reduce running
activity (closing an app, stopping or disabling its process).

## 2. Who writes `entity_memory`

Every write happens inside a turn or a person's action. No background pass reads conversations
afterwards to decide what to keep.

| Writer | Trigger | memory_type |
|---|---|---|
| `brain/mastercode/memory_engine.py::write_turn_row`, `write_turn_row_by_token` | every user message and reply | `turn` |
| `server/synaptive_handler.py::handle_synaptive_signal` | an external client posts the turn it just produced | `turn` |
| `server/connectors/thread_pick.py::write_pick_marker`, `write_topic_marker` | a thread or topic pick | `turn` (marker row) |
| `brain/mastercode/runtime/thread_choice.py::_marker` | the Host picks the thread a message lands in | `turn` (marker row) |
| `brain/mastercode/circles_threads.py::_split_in_transaction` | the person splits a thread | `turn` (marker row) |
| `brain/mastercode/runtime/turn_home.py::rehome_turn` | a turn moved to another thread | `turn` |
| `brain/mastercode/runtime/tools.py::_write_started_from` | a conversation started from another | `turn` (marker row) |
| `server/routes/projects.py::adopt_thread_core` | a thread adopted into a project | `turn` |
| `brain/mastercode/runtime/close_turn.py::_write_preference` | the closing note of a reply names what the person wants | `preference` |
| `brain/mastercode/runtime/close_turn.py::_write_goal` | the closing note names the conversation's goal | `goal` |
| `brain/mastercode/runtime/tools.py::_curate_add`, `_curate_add_slot`, `_curate_replace` | the Host calls `memory_curate` | `preference` or `relationship` |
| `brain/mastercode/memory_engine.py::_store_teaching_memory` | a message that teaches a rule ("Remember this:" and similar; `_detect_teaching`, called from `runtime/session.py`) | `preference` (a taught rule) |
| `brain/mastercode/runtime/task_record.py::_promote` | a task's finding promoted to memory | `cited_fact` |
| `brain/mastercode/skills/deep_research.py::_persist` | a deep-research run finishes | `research` |
| `server/routes/context.py::promote_context_service` | the person or the `context_ingest` tool promotes a staged document | `research` by default |
| `brain/mastercode/memory_engine.py::write_governance_event` | the crisis gate flags a message | `governance_observation` |
| `server/routes/brain.py::edit_single_memory` | the person corrects an entry in the Memory app | the edited row's type |
| `brain/mastercode/circles_threads.py::_own_words` | deleting a thread when another live conversation said one of its rows in other words | the archived row's type |

### Slots

`migrations/v241_memory_slots.sql` gives `entity_memory` a `slot_topic`, a `slot_scope` and a
`turn_type`. A unique partial index allows one live entry per type, scope and topic. A write to
an occupied slot (`brain/mastercode/memory_slots.py::write_in_slot`) supersedes the older entry
in the same transaction, so the newest entry for a topic is the live one. `memory_curate` and
close_turn's preference writer use it when the Host names a topic; `memory_curate op='key'`
gives an existing row a topic. A row with no topic is outside the slot rule.

## 3. Who reads `entity_memory`

| Reader | Output goes to |
|---|---|
| the memory engine's `build_memory_context`, called by `runtime/prompt_schema.py::_b_memory` | the `memory` block, per turn |
| `brain/mastercode/constitution/context.py::build_context_block` | the "What applies right now" block, per turn |
| `brain/mastercode/runtime/close_turn.py::thread_state` | the `thread_state` block, per turn |
| `brain/mastercode/memory_engine.py::recall_tool_trail` | the `tool_trail` block, per turn |
| the `recall` tool, through `brain/mastercode/memory_retrieval.py::unified_search` | the tool result |
| `brain/mastercode/knowledge_toc.py::render_block`, `build_toc` | the `knowledge_toc` block (Circles) |
| `server/synaptive_handler.py::recent_identity_memories`, `handle_synaptive_recall` | an external client's prompt and recall |
| the Memory app routes in `server/routes/brain.py` (`GET /memories`, `/memories/stats`, `/memories/{id}`) | the Memory app |

## 4. The memory types

`migrations/v235_one_memory.sql` sets the CHECK to these:

| Type | What it holds |
|---|---|
| `turn` | one message of the transcript, or a marker a thread operation leaves |
| `summary` | a rolling summary of a Circles conversation. Nothing writes it now: v241 retired the summary writer and archived the live rows |
| `preference` | what the person wants; a rule they taught is a preference with `metadata.rule_kind` |
| `relationship` | a note about someone in the person's life |
| `constitutional` | still allowed; v236 archived the platform-value rows (the values render from code) and turned a Host's own rule rows into preferences with `rule_kind` 'taught' |
| `goal` | the goal of one thread, open or resolved |
| `cited_fact` | what a source said, in its own words, with where and when it was read |
| `research` | the product of a research run or a promoted document |
| `governance_observation` | the crisis gate's record that a message was flagged |
| `retired` | a row of a removed type, archived, with the old name in `metadata.prior_memory_type` |

v235 retired `fact`, `decision`, `topic`, `emotion`, `unresolved`, `knowledge`, `skill_memory`,
`moral_precedent` and `legacy`: their rows became `retired` and archived, so the Memory app's
as-of view still shows them. What the person says about themselves is a preference the Host
keeps in the turn.

## 5. The system prompt

### The blocks, in render order

`PROMPT_SCHEMA` (`brain/mastercode/runtime/prompt_schema.py`) declares fourteen blocks. They
render STATIC first, then PER_REBUILD, then a `<!-- CACHE_BREAK -->` marker, then PER_TURN.
Everything before the marker is the cached prefix.

| Block | Freshness | What it carries |
|---|---|---|
| `constitutional` | STATIC | the platform's five values, from code |
| `communication_discipline` | STATIC | the medium's constraints and the safety principles |
| `identity` | PER_REBUILD | the Host's name, its directive, the tendencies its sliders render and the explanation level |
| `surface_role` | PER_REBUILD | the app the person is in |
| `map` | PER_REBUILD | counts: tool groups, apps, machines, projects, repos, skills |
| `comm_context` | PER_REBUILD | who the Host is talking to and the time-zone rule |
| `frame` | PER_TURN | this turn: surface, project, thread, attachments, open view, the clock |
| `knowledge_toc` | PER_TURN | Circles only: what the thread holds and how to open it |
| `timeline` | PER_TURN | the Host's work running now, just finished and next |
| `memory` | PER_TURN | §3, first row |
| `app_state` | PER_TURN | what the platform last measured about the Host's apps |
| `appearance` | PER_TURN | the desktop the Host designed and whether it displays |
| `diagnostics` | PER_TURN | measured faults on the Host's machine with a fix at hand |
| `live_context` | PER_TURN | the cards this message scores |

### What the session adds

`brain/mastercode/runtime/session.py` adds these outside the schema, and logs each to
`prompt_log` so the prompt record shows every byte: `host_rules` (the person's own rules for
this Host), `orchestrator_catalog`, `turn_type_skill`, `crisis_check` (the crisis gate flagged
the message; the reply still runs), `model_style`, `constitution_context`, `inbox`,
`thread_state`, `tool_trail` and `own_skills`.

## 6. Gates

- `scripts/test_host_world_pg.py` builds the kernel schema on a real Postgres and checks the
  `host_world` role and its walls.
- `scripts/test_cache_stable_prefix.py` checks how the cached prefix is built and that
  `prompt_log` carries a hash per rendered block.
- `scripts/test_prompt_provenance.py` checks that injected blocks are logged to `prompt_log`.
- `scripts/test_no_hand_kernel_tables.py` fails when a gate builds a kernel table by hand
  instead of taking the schema from `scripts/kernel_schema.py`.
- `scripts/audit_orphan_tables.py` reports tables declared with no runtime reference.
