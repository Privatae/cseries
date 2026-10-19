-- v149: prompt_log carries the THREAD the build was for.
--
-- NUMBERED 149, NOT 148: 148 is v148_inference_cache_tokens.sql, and
-- scripts/test_migration_versions.py reports no collisions. The version
-- INTEGER, not the filename, is a migration's primary key — entrypoint.sh
-- reduces the name to it and SILENTLY skips a file whose number is already in
-- schema_migrations, so two files sharing one number
-- means one of them never runs on any host that has seen the other, and never
-- says so.
--
-- THE DEFECT. The thread-context panel ("what the next turn fires with",
-- brain/mastercode/context_budget.py:617 build_thread_context, served to both
-- the workbench /api/brain/thread-context and circles
-- /api/circles/thread-context/{kind}/{id}) fills its `blocks` and
-- `last_prefired` sections from latest_prompt_blocks (context_budget.py:256),
-- whose read was `SELECT scope, blocks FROM prompt_log WHERE entity_id=$1
-- ORDER BY log_id DESC LIMIT 40` — entity-scoped and nothing else. With two
-- threads open the panel showed whichever thread had sent last: open the
-- panel on thread A and read thread B's pre-fired memories.
--
-- WHY THE ROW COULD NOT ANSWER IT. v013_prompt_log.sql:13 declares exactly
-- seven payload columns — entity_id, session_id, scope, block_count,
-- total_chars, has_cache_break, blocks — and not one of them identifies the
-- conversation. session_id is the closest thing and it is not it: the chat
-- path is sessionless (the transient EntitySession of the no-sessions
-- architecture), so one session id spans whatever threads that process serves,
-- and log_injected_block rows inherit the same id. There was no key to filter
-- on, so this column is what adds one.
--
-- WHAT GOES IN IT. The SAME `{kind, ref}` descriptor the turn rows already
-- carry at metadata->'source_context' — produced by ONE function,
-- brain/mastercode/memory_engine.py:1013 derive_source_context, and backfilled
-- onto turn rows by v050_source_context_backfill.sql. kind ∈ {project, app,
-- peer, thread, session}. `thread` is a Circles thread: ref is a
-- circles_threads.id and it is matched EXACTLY, never as a suffix on
-- app:circles, because the app branch of the turn matcher also accepts the
-- legacy metadata.app_id tag and a suffixed ref would leak every thread's
-- turns back into the original conversation. A new kind with no pre-existing
-- rows needs no backfill; the NULL rule below is unchanged by it.
-- Reusing the descriptor rather than inventing a prompt_log
-- thread key is the point: the panel's other three sections (history_preview,
-- summary, budget) are already scoped by this exact descriptor, passed in from
-- the route handlers (server/routes/circles.py:1153 _thread_src_ctx,
-- server/routes/brain.py:456), so the blocks section now agrees with them
-- instead of answering a different question.
--
-- JSONB, not two TEXT columns, for the same reason: `source_context = <the
-- turn row's source_context>` has to stay a sentence you can write, and a
-- reader that splits the descriptor in half here would be free to drift from
-- the one in entity_memory.
--
-- NULLABLE, no default, no backfill — all three deliberate:
--   * Every prompt_log row written before this migration is genuinely of
--     unknown thread. There is no signal to recover it from (that is the
--     defect), so inventing one would be fabrication, and the obvious guess —
--     "it was circles" — is precisely the cross-thread leak being fixed.
--   * NULL is therefore read as "not this thread, and not any thread": the
--     per-turn filter in latest_prompt_blocks excludes it, so a thread with no
--     build of its own reads EMPTY rather than borrowing another thread's
--     pre-fire. Empty is a state the panel already renders
--     (build_thread_context's docstring: every section is independently
--     best-effort and a missing piece reads as null/empty).
--
--     For ONE thread kind that emptiness is not transient and must not be
--     described as though it were. A circles dm/entity thread maps to
--     kind=peer (server/routes/circles.py:1153 _thread_src_ctx) and NOTHING
--     ever writes a peer-stamped prompt_log row: derive_source_context's
--     `peer_ref` parameter (memory_engine.py:1018) has zero call sites, and a
--     peer turn is INGESTED from the other host (server/synaptive_handler.py)
--     rather than built here, so no local prompt build exists to log. A peer
--     thread therefore has no per-turn blocks and no pre-fire, permanently and
--     by design — which is why the payload names that case rather than
--     returning a bare empty list that reads as a bug (build_thread_context's
--     `prefire_state`).
--   * The static+per_rebuild rows stay entity-wide by design — they are the
--     cached system message, identical whatever thread is open — so the
--     filter applies to per_turn rows ONLY, and window_report
--     (context_budget.py:531, the host's own mirror of its last turn) keeps
--     reading the table entity-scoped with no descriptor at all.
--
-- The write stays best-effort: _write_prompt_log (prompt_schema.py:1117)
-- swallows its own failure, because a provenance row must never fail a turn.
--
-- NO to_regclass GUARD, DELIBERATELY. prompt_log is unconditionally declared by
-- migrations/v013_prompt_log.sql:13 (`CREATE TABLE IF NOT EXISTS prompt_log`),
-- and entrypoint.sh applies migrations in version order, so v013 has run on
-- every host before this file is reached — the v147 rule, that a table is
-- either declared in migrations/ or it is not ours to assume. A guard here
-- would be theatre: the COMMENT and the CREATE INDEX below cannot be wrapped in
-- the same DO block's exception, so guarding only the ALTER is the v080
-- crashloop shape with extra steps — the table is either there for all three
-- statements or the migration should fail loudly on the first one under
-- ON_ERROR_STOP=1. Applied clean against cseries-postgres:smoke with init-db.sql
-- + every migration. (The guard lint,
-- scripts/test_migrations_guarded.py, polices files that opt into
-- `ALTER TABLE IF EXISTS`; this one deliberately does not.)

ALTER TABLE prompt_log
    ADD COLUMN IF NOT EXISTS source_context JSONB;

COMMENT ON COLUMN prompt_log.source_context IS
    'The thread this prompt build was for, as the unified {kind, ref} '
    'descriptor turn rows carry at metadata->''source_context'' '
    '(memory_engine.derive_source_context). Set on per-turn builds; NULL on a '
    'build with no thread (session start, voice/cron/background) and on every '
    'row predating v149. NULL is never "any thread" — the per-turn read '
    'excludes it rather than showing it to a thread it may not belong to.';

-- The index the panel's read actually walks. Both forms of that read —
-- filtered and not — are `WHERE entity_id=$1 ... ORDER BY log_id DESC
-- LIMIT 40`, so what they need is a backwards walk on log_id within one
-- entity, with the source_context predicate applied as a filter along the way.
-- v013 indexes (entity_id, created_at DESC), which is a different column and
-- cannot serve that ordering.
--
-- NOT an index on source_context, and this was MEASURED rather than argued.
-- The obvious (entity_id, kind, ref, log_id DESC) WHERE source_context IS NOT
-- NULL cannot serve the query: the filtered read's first arm is `scope <>
-- 'per_turn'`, which selects exactly the rows the partial predicate excludes
-- (static rows are unstamped), so the index is unusable for it. Confirmed on
-- cseries-postgres:smoke with 220k prompt_log rows across 50 entities,
-- With ONLY the partial index present the planner ignored it and
-- fell back to a primary-key scan discarding 13,859 rows (1.02 ms); with the
-- index below it took `Index Scan using idx_prompt_log_entity_log_id,
-- Index Cond: (entity_id = 9003)` and discarded 238 (0.30 ms). The partial index
-- would have been write amplification on every prompt build, bought for
-- nothing.
--
-- Honest limit of that measurement: on a database holding ONE entity (the
-- per-pod case) the planner prefers the primary key's own backward walk and
-- does not use this index either — log_id and the pkey are the same ordering,
-- and with no other entity to skip there is nothing for the entity_id prefix
-- to do. It earns its keep where prompt_log holds several entities, and costs
-- one btree insert per prompt build where it does not.
CREATE INDEX IF NOT EXISTS idx_prompt_log_entity_log_id
    ON prompt_log (entity_id, log_id DESC);
