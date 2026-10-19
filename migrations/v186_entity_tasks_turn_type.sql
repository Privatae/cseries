-- v186 — an agent, build or check records the turn type of the turn that
-- started it.
--
-- The timeline block shows every turn what is running, what just finished and
-- what is next, across types. Each line names the type the work runs under,
-- and a turn sees an item's own words (its title) only when the item is of
-- that turn's type: across types, only platform facts cross. This column is
-- where that type is read from. NULL is work started untyped, or before this.

BEGIN;

ALTER TABLE entity_tasks
    ADD COLUMN IF NOT EXISTS turn_type_id UUID NULL REFERENCES turn_types(id) ON DELETE SET NULL;

COMMIT;
