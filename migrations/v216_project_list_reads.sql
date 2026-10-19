-- v216: what the projects list (GET /api/projects) and the
-- project search read, which the Workbench's tabs poll.
--
-- Nearly all of the list's time went to reads it makes for every project that
-- nothing served: _LAST_ACTIVITY_SQL's newest turn row and newest task
-- (server/routes/projects.py), matched on project_id and result->>'project_id'
-- with no index, so every project read all of the entity's turn rows or tasks
-- again; and last_landed_at's read of every Host reply, whose landed time was
-- a key inside the reply's metadata (a few kB, most of it stored out of line),
-- so each reply's metadata was fetched and parsed. Neither field's value
-- changes here.
--
-- 1. entity_memory.landed_at: when a turn row's words were last written. The
--    two turn writers set it at every write and rewrite of a row
--    (memory_engine._write_turn_row_landed, _write_turn_row_by_token_landed),
--    never earlier than created_at, which stays when the words were said. It
--    replaces the metadata key landed_epoch, which no live row carries (the
--    writers that stamped it never shipped). Rows written before this
--    keep it NULL: nothing fills it, and they are read by created_at,
--    COALESCE(landed_at, created_at). Nullable with no default, so adding it
--    rewrites no row.
-- 2. idx_entity_memory_project_turns: a project's turn rows by time. The
--    newest is one index entry, read without touching the table.
-- 3. idx_entity_memory_project_replies: the Host's replies in a project
--    (metadata.role 'assistant') by COALESCE(landed_at, created_at). A
--    project's last landed reply is one index entry and one row read
--    (_PAGE_MARKS_JOINS).
-- 4. idx_entity_tasks_project: a project's tasks by the time
--    _LAST_ACTIVITY_SQL takes from each, COALESCE(completed_at, started_at,
--    created_at). The newest is one index entry.
--
-- The planner uses an index only when the query's expressions match it, so
-- these expressions must stay the ones projects.py writes;
-- scripts/test_project_list_speed_pg.py fails when the list or the search
-- stops using them.
--
-- One transaction and no CONCURRENTLY. Migrations run at boot, before the
-- server takes requests, each file through `psql -f` with no transaction
-- around it (entrypoint.sh), so no request waits on the build, and a boot
-- stopped part way leaves none of it. CONCURRENTLY cannot run inside a
-- transaction, and an interrupted concurrent build leaves an invalid index
-- that IF NOT EXISTS would skip on every later boot.
--
-- Idempotent (IF NOT EXISTS). Declared for fresh Hosts in init-db.sql too.

BEGIN;

ALTER TABLE entity_memory ADD COLUMN IF NOT EXISTS landed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_entity_memory_project_turns
    ON entity_memory (entity_id, project_id, created_at)
    WHERE memory_type = 'turn' AND project_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_entity_memory_project_replies
    ON entity_memory (entity_id, project_id, (COALESCE(landed_at, created_at)))
    WHERE memory_type = 'turn' AND project_id IS NOT NULL
      AND (metadata->>'role') = 'assistant';

CREATE INDEX IF NOT EXISTS idx_entity_tasks_project
    ON entity_tasks (entity_id, (result->>'project_id'),
                     (COALESCE(completed_at, started_at, created_at)))
    WHERE (result->>'project_id') IS NOT NULL;

COMMIT;
