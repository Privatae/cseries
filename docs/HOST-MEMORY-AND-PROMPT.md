# The Host's memory and prompt (2026-10-17)

What the platform keeps for a Host, what the Host keeps for itself, every writer and reader of
`entity_memory`, and the blocks of the system prompt. Every `file:line` was read on 2026-10-17
and may have moved since.

> In this document "the platform" means the C-Series kernel inside the Host: the code outside
> the Host's own `world` schema. It does not mean a hosted service; C-Series has no hosted Host.

## 1. What the platform owns and what the Host owns

The platform owns four things:

- **The walls:** the capability gate, turn types, the root turn, approvals, the `host_world` role
  and the operator kill switch.
- **The transcript:** every turn, the rolling summary of what overflowed, and the markers a thread
  operation leaves (pick, index, split, rehome, started-from).
- **The person's standing instructions:** their preferences and rules, their ten personality
  sliders and explanation level, and the platform's five values.
- **The facts it measures about this turn:** frame, timeline, app state, diagnostics, live cards.

The Host owns the schema `world`. It creates the tables, views and functions a request needs
there, through the `data` tool (`brain/mastercode/runtime/tools.py:1297-1302`, which hands every
op to `world_db.run_op`). The tool runs the Host's SQL as the role `host_world`
(`migrations/v233_host_world.sql:1-15`), so Postgres enforces the wall whatever SQL the model
writes: the role owns `world`, reads only the kernel tables on the READABLE list
(`v233_host_world.sql:246`), and writes nothing outside `world`. The one thing the kernel requires
of the Host is a COMMENT on every object saying what it is for; a transaction that leaves one
without is rolled back (`brain/mastercode/world_db.py:10-12`). Before a call that could remove or
reshape data, `world_db` takes a `pg_dump -n world` snapshot, and a restore lands in one
transaction (`world_db.py:15-17`). `migrations/v234_world_recall.sql` adds `world_catalog_vectors`
(the embedding of each object's purpose line, so recall can search `world` by meaning) and
`memory_recall_aliases` (words the Host adds to a memory so recall finds it by them,
`memory_curate op='alias'`). The `world` skill (`default-skills/world.md`) is where the Host reads
how to use it.

A shaped fact about the person (the tools in their kitchen, a habit they are building) belongs in
a `world` table. A one-line note about the person is a preference.

### The kill switch

While the file `/etc/privatae/ceigas_kill_switch` exists, every crossing that does not touch
`core.governance` is refused, except a short list of crossings that only reduce running activity
(closing an app, stopping or disabling its process), so an emergency stop never blocks stopping
things (`brain/ceigas_kill_switch.py:27`, `:55-75`). A file keeps the state outside any one
process: it survives a restart and needs no database to set.

## 2. Who writes `entity_memory`

Every write happens inside a turn or a person's action. No background pass reads conversations
afterwards, and no second model decides what to keep. close_turn, `memory_curate` and
`task_record` write through one helper, `MemoryConsolidator._insert_new_memory` (`brain/mastercode/memory_consolidation.py:431`, inserts at
`:470` and `:485`).

| Writer | Trigger | memory_type | Scope |
|---|---|---|---|
| `write_turn_row` / `write_turn_row_by_token` (`brain/mastercode/memory_engine.py:1638`, `:1831`; INSERT `:1791`, `:1808`) | every user message and reply (`runtime/session.py:5527`, `:5549`, `:3328`) | `turn` | the place the turn happened, `metadata.source_context` (`memory_engine.py:1823`) |
| `handle_synaptive_signal` (`server/synaptive_handler.py:682`; INSERT `:848`, `:861`) | an external client posts the turn it just produced | `turn` | — |
| `write_pick_marker`, `index_marker_text`, `write_topic_marker` (`server/connectors/thread_pick.py:731`, `:807`, `:1329`) | a thread or topic pick | `turn` (marker row) | — |
| `_split_in_transaction` (`brain/mastercode/circles_threads.py:1895`; INSERT `:2050`) | the person splits a thread | `turn` (marker row) | — |
| `rehome_turn` (`brain/mastercode/runtime/turn_home.py:148`; INSERT `:217`) | a turn moved to another thread | `turn` | — |
| `_write_started_from` (`runtime/tools.py:20254`; INSERT `:20280`) | a conversation started from another | `turn` (marker row) | — |
| `adopt_thread_core` (`server/routes/projects.py:910`; INSERT `:1185`) | a thread adopted into a project | `turn` | — |
| `_store_summary` (`brain/mastercode/thread_consolidator.py:343`; INSERT `:401`) | after a Circles turn, when the history passes 60% of its budget or 60 turns have passed since the last summary (`thread_consolidator.py:81-82`) | `summary` | that Circles conversation (`brain/mastercode/summary_policy.py:14-19`) |
| `_write_preference` (`brain/mastercode/runtime/close_turn.py:367`; insert `:462`) | the closing note of a reply names what the person wants | `preference` | the turn's place |
| `_write_goal` (`close_turn.py:679`; insert `:698`) | the closing note names the conversation's goal | `goal` | the turn's place |
| `_curate_add`, `_curate_replace` (`runtime/tools.py:8107`, `:8192`; insert `:8139`, `:8262`) | the Host calls `memory_curate` | `preference` or `relationship` (`tools.py:7942`) | the turn's `source_context` (`tools.py:8136-8137`); widened later by `memory_scope` |
| `_store_teaching_memory` (`memory_engine.py:2642`; INSERT `:2750`) | a message starting "Remember this:" and similar (`_detect_teaching`, `memory_engine.py:2594`; called at `session.py:5569-5583`) | `preference` (a taught rule) | where it was said |
| `_promote` (`brain/mastercode/runtime/task_record.py:968`; insert `:1023`) | a task's finding promoted to memory | `cited_fact` | the project (`task_record.py:1019`), or everywhere when marked so (`:1020-1021`) |
| `_persist` (`brain/mastercode/skills/deep_research.py:125`; INSERT `:156`, `:181`) | a deep-research run finishes | `research` | the run's project, when it has one (`deep_research.py:158-161`) |
| `promote_context_service` (`server/routes/context.py:1735`; INSERT `:1825`) | the person or the `context_ingest` tool promotes a staged document | `research` by default; `preference` or `relationship` when asked | — |
| `write_governance_event` (`memory_engine.py:1974`; INSERT `:2009`) | the crisis gate flags a message (`session.py:1484`) | `governance_observation` | the session (`source_session`, `memory_engine.py:2009-2016`) |
| `edit_single_memory` (`server/routes/brain.py:1716`; INSERT `:1782`, `:1797`) | the person corrects an entry in the Memory app | the edited row's type | the edited row's |
| `_own_words` (`circles_threads.py:1357`; INSERT `:1479`) | deleting a thread (`_forget`, `circles_threads.py:804`, call `:893`) when another live conversation said one of its rows in other words | the archived row's type, one of the restatement types (`memory_engine.py:2029`) | that other conversation |

## 3. Who reads `entity_memory`

| Reader | Reads | Output goes to |
|---|---|---|
| `build_memory_context` (`memory_engine.py:3360`), called by `_b_memory` (`runtime/prompt_schema.py:749`) | the thread audit's open findings; the taught rules ("Things You Were Taught", `:3526`); a per-turn search over preferences and relationship notes against the message ("Relevant Right Now", `:3623`); the standing preferences and relationship notes (`:3690-3693`; types `PROMPT_MEMORY_TYPES`, `:1038`) | the `memory` block, per turn |
| `build_context_block` (`brain/mastercode/constitution/context.py:35`) | the person's rules that ride everywhere, matched to the message by meaning, or by importance when the turn has no vector | the "What applies right now" block, per turn (`session.py:6096-6109`) |
| `close_turn.thread_state` (called at `session.py:6312-6317`) | the thread's goal and last handoff | the `thread_state` block, per turn |
| `get_live_summary` (`thread_consolidator.py`, called at `session.py:6302-6310`) | the live summary of a Circles conversation | the history's lead message |
| `recall_tool_trail` (`memory_engine.py:1579`) | the tool calls on earlier turn rows | the `tool_trail` block, per turn |
| the `recall` tool (`session.py:9624`) via `unified_search` (`brain/mastercode/memory_retrieval.py:1112`) | memory, documents, projects, code, skills and `world` (`memory_retrieval.py:871`) | the tool result |
| `knowledge_toc.render_block` / `build_toc` (`brain/mastercode/knowledge_toc.py:926`, `:757`) | counts of what a Circles thread holds (turns, goals, artifacts, tool trail) | the `knowledge_toc` block; the full index through `chat_threads op='toc'` |
| `recent_identity_memories` (`server/synaptive_handler.py:490`) | preferences and relationship notes touched in the last 48 hours, global ones only, never a rule | an external client's identity prompt |
| `handle_synaptive_recall` (`synaptive_handler.py:308`) | a similarity search over `entity_memory` | an external client's recall result |
| the Memory app routes (`server/routes/brain.py`: `GET /memories` `:935`, `/memories/stats` `:1086`, `/memories/{id}` `:1228`) | every row, by Canonical, All or As-of | the Memory app |

## 4. The memory types

`migrations/v235_one_memory.sql:75-78` narrows the CHECK to these:

| Type | What it holds |
|---|---|
| `turn` | one message of the transcript, or a marker a thread operation leaves |
| `summary` | the rolling summary of what overflowed a Circles conversation |
| `preference` | what the person wants; a rule they taught or the Host learned is a preference with `metadata.rule_kind` |
| `relationship` | a note about someone in the person's life |
| `constitutional` | a Host's own standing rule row; v236 retypes the ones a Host held as preference rules |
| `goal` | the goal of one thread, open or resolved |
| `cited_fact` | what a source said, in its own words, with where and when it was read |
| `research` | the product of a research run or a promoted document |
| `governance_observation` | the crisis gate's record that a message was flagged |
| `retired` | a row of a removed type, archived, with the old name in `metadata.prior_memory_type` |

Retired on 2026-10-17: `fact`, `decision`, `topic`, `emotion`, `unresolved`, `knowledge`,
`skill_memory`, `moral_precedent` and `legacy`. Their writers were a second model reading the
transcript after the fact and deciding what to remember, and that decision (2026-09-30) is that
every memory operation is a tool call in the Host's own turn. Most of those rows were reachable only
through `recall`, and a fact with a shape of its own is better held in a table the Host designs in
`world`. v235 archives their rows as `retired` (`v235_one_memory.sql:61-72`), so the Memory app's
As-of view still shows them, and drops the ten-dimension personality vector and its companions from
`entity_memory`.

## 5. The system prompt

### The blocks, in render order

`PROMPT_SCHEMA` (`brain/mastercode/runtime/prompt_schema.py:812-909`) declares fourteen blocks. They
render STATIC first, then PER_REBUILD, then a `<!-- CACHE_BREAK -->` marker, then PER_TURN
(`prompt_schema.py:1071-1079`). Everything before the marker is the cached prefix.

| Block | Freshness | What it carries |
|---|---|---|
| `constitutional` | STATIC | the platform's five values, from code (`brain/mastercode/constitution/platform_values.py`) |
| `communication_discipline` | STATIC | the medium's constraints, one honesty line and the safety principles (`runtime/prompt_builder.py:416`) |
| `identity` | PER_REBUILD | the Host's name, its directive, the tendencies its sliders render (`prompt_builder.py:26`) and the explanation level (`prompt_builder.py:360`) |
| `surface_role` | PER_REBUILD | "Where You Are": the app the person is in, with that app's one-paragraph `what_it_is` (`prompt_builder.py:902`) |
| `map` | PER_REBUILD | one screen of counts: tool groups, apps, machines, projects, repos, skills |
| `comm_context` | PER_REBUILD | who the Host is talking to and the time-zone rule (`prompt_builder.py:266-279`) |
| `frame` | PER_TURN | this turn: surface, project, thread, attachments, open view, the clock |
| `knowledge_toc` | PER_TURN | Circles only: counts of what the thread holds and how to open it |
| `timeline` | PER_TURN | the Host's work running now, just finished and next |
| `memory` | PER_TURN | §3, first row |
| `app_state` | PER_TURN | what the platform last measured about the Host's shipped apps |
| `appearance` | PER_TURN | the desktop the Host designed and whether it displays |
| `diagnostics` | PER_TURN | measured faults on the Host's machine with a fix at hand |
| `live_context` | PER_TURN | the cards this message scores, read by code this turn |

### What the session adds

`runtime/session.py` adds these outside the schema; each is logged to `prompt_log` by
`log_injected_block`, so the prompt record shows every byte:

| Block | Where it rides | Line |
|---|---|---|
| `host_rules` | the person's own rules for this Host, placed first in the system message | `session.py:7094` |
| `orchestrator_catalog` | tool lane counts and the build doors, appended to the cached system message | `session.py:7090` |
| `turn_type_skill` | the turn type's skill, after the catalog it governs | `session.py:7092` |
| `crisis_check` | the fact that the crisis gate flagged the message; the reply still runs | `session.py:1497` |
| `model_style` | the calibration for the serving model, where one is registered | `session.py:6085` |
| `constitution_context` | "What applies right now", §3 | `session.py:6109` |
| `inbox` | unread "Messages For You" | `session.py:5878`, `:6124` |
| `thread_state` | the thread's goal and last handoff | `session.py:6491` |
| `tool_trail` | the tool calls of earlier turns in the thread | `session.py:6482` |
| `own_skills` | up to two of the Host's own skills that match the message | `session.py:7045` |

## 6. What the gates guarantee

- `scripts/test_host_world_pg.py` builds the kernel schema the way the boot does (init-db.sql, then
  every migration, then v233) on a real Postgres, and checks the `host_world` role, its walls, the
  three lists in v233, the executor, snapshots and restore.
- `scripts/test_cache_stable_prefix.py` fails when the cached part of the prompt carries anything
  that moves turn to turn, and checks that `prompt_log` holds a content hash per block.
- `scripts/audit_orphan_tables.py` reports tables declared with no runtime reference, names
  referenced but never declared, and retired tables left behind; it reports, it never prescribes a
  drop.
- `scripts/test_no_hand_kernel_tables.py` fails when a gate creates, patches or loosens a kernel
  table by hand instead of taking the schema from `scripts/kernel_schema.py`.
- `scripts/test_prompt_provenance.py` checks that `log_injected_block` writes a real `prompt_log`
  row, that the per-turn rules block is always logged, that turn-path failures log an error with the
  Host's id, and that the personality expression block and package stay gone.
