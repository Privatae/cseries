-- v184 — "Once" on a turn type.
--
-- A widening card offers Once beside From now on: the NEXT turn of this type
-- gets the asked-for items, and the type itself is unchanged. The grant waits
-- here until a turn of the type starts, which takes it and clears it in one
-- statement (runtime/turn_type_approval.consume_once). Written only by the
-- ticket-decide route, on the person's click.

BEGIN;

ALTER TABLE turn_types
    ADD COLUMN IF NOT EXISTS once_grant JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMIT;
