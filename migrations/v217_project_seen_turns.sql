-- v217: the replies the person watched arrive on a project's page.
--
-- projects.seen_turns: the turn tokens of the replies the person watched
-- arrive on the page (its turn ended while the page was in front), newest
-- last, at most 32. Set by PUT /api/projects/{id}/seen with {"turn_token"}
-- (server/routes/projects.py mark_project_seen_service), together with
-- seen_at.
--
-- Why a token and not a time: a reply's row is written after its stream has
-- ended, in a task of its own that first embeds the text
-- (memory_engine._write_turn_row_landed); a failing embedder holds the write
-- about 92 s, and in PII mode the drain worker writes it later still. So the
-- reply the person watched lands AFTER the mark the page sent when the turn
-- ended, and a time mark alone cannot tell it from something new. The row
-- carries its turn's token (metadata.turn_token, session.py _turn_tags), so a
-- landing after seen_at lights the page's tab unless its token is one of
-- these (projects._PAGE_MARKS_JOINS, unseen_landed_at). Everything else that
-- lands after the mark still lights it: an agent's report carries no token,
-- another turn's reply carries its own, and a finished task has none.
--
-- 32 is far more than can wait at once: a token matters only until its
-- reply's row has landed and a later mark has passed it, and every token
-- here is a turn the person sent and watched end.
--
-- Nullable, no default: adding it rewrites no row. Idempotent: ADD COLUMN IF
-- NOT EXISTS, and the CHECK is added only where it is missing, so a fresh
-- Host keeps the one init-db.sql declares (scripts/test_workbench_seen_mark_pg
-- .py holds the two equal). One transaction, so a boot stopped part way
-- leaves none of it.

BEGIN;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS seen_turns TEXT[];

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'projects_seen_turns_check'
                      AND conrelid = 'projects'::regclass) THEN
        ALTER TABLE projects ADD CONSTRAINT projects_seen_turns_check
            CHECK (seen_turns IS NULL
                   OR (cardinality(seen_turns) <= 32
                       AND array_position(seen_turns, NULL) IS NULL));
    END IF;
END $$;

COMMIT;
