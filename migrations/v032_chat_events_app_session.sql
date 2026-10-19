-- v032: chat_events gains app_id + cc_session_id + originating_app
--
-- Background: the chat path is moving to a unified log across apps
-- (Circles, AppFactory, Publisher, etc) with SDK session continuity
-- tracked per row. Three columns:
--
--   app_id            which app surface the message arrived in
--   cc_session_id     the claude_agent_sdk session_id (for resume=...)
--   originating_app   the app the user was IN when they sent it
--                     (may differ from app_id if a tool/redirect moved
--                     the message to another surface)
--
-- All nullable; existing rows backfill to the Circles defaults so the
-- audit log keeps reading. The (entity_id, cc_session_id) index serves
-- the resume-policy lookup ("last assistant turn in this thread within
-- 30 min → continue that session").

BEGIN;

ALTER TABLE chat_events
    ADD COLUMN IF NOT EXISTS app_id            TEXT,
    ADD COLUMN IF NOT EXISTS cc_session_id     TEXT,
    ADD COLUMN IF NOT EXISTS originating_app   TEXT;

-- Backfill pre-existing rows. Circles is the only chat surface that
-- has ever written to chat_events, so attribution is safe.
UPDATE chat_events
   SET app_id          = COALESCE(app_id, 'circles'),
       originating_app = COALESCE(originating_app, 'circles')
 WHERE app_id IS NULL OR originating_app IS NULL;

CREATE INDEX IF NOT EXISTS chat_events_resume_idx
    ON chat_events (entity_id, cc_session_id, created_at DESC)
 WHERE cc_session_id IS NOT NULL;

COMMIT;
