-- Migration v083: ensure calendar_events has the event_id UNIQUE
-- constraint that init-db.sql:1705 declares.
--
-- init-db.sql gained `event_id UUID ... NOT NULL` + `CONSTRAINT
-- calendar_events_event_id_key UNIQUE (event_id)` (Ship E, PME<->EME
-- dual-write idempotency), but no migration ever shipped the constraint
-- to pods whose calendar_events predates it. On those pods every
-- bridge calendar.append (server/bridge/memory_handlers.py:256,
-- `ON CONFLICT (event_id) DO NOTHING`) fails with
--   there is no unique or exclusion constraint matching the ON CONFLICT
--   specification
-- — observed on a live Host's boot 2026-07-09; the write is lost and the
-- bridge logs a full traceback on every calendar.log dispatch.
--
-- Handles all pod generations, idempotently:
--   1. pods missing the event_id column entirely -> add it
--   2. backfill NULL event_ids, then dedupe (keep oldest row per id)
--   3. add the UNIQUE constraint if absent (name matches init-db.sql
--      so fresh pods no-op)

BEGIN;

-- 1. Column (very old pods). Same shape as init-db.sql:1700.
ALTER TABLE calendar_events
    ADD COLUMN IF NOT EXISTS event_id UUID DEFAULT gen_random_uuid();

-- 2a. Backfill any NULLs (rows inserted before the column had a default).
UPDATE calendar_events SET event_id = gen_random_uuid() WHERE event_id IS NULL;

-- 2b. Dedupe: without the constraint, retried dual-writes could have
-- landed the same event_id twice. Keep the oldest physical row.
DELETE FROM calendar_events a
      USING calendar_events b
      WHERE a.event_id = b.event_id
        AND a.ctid > b.ctid;

-- 3. NOT NULL + the constraint, only where missing.
DO $$
BEGIN
    ALTER TABLE calendar_events ALTER COLUMN event_id SET NOT NULL;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'calendar_events_event_id_key'
           AND conrelid = 'calendar_events'::regclass
    ) THEN
        ALTER TABLE calendar_events
            ADD CONSTRAINT calendar_events_event_id_key UNIQUE (event_id);
    END IF;
END $$;

COMMIT;
