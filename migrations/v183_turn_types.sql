-- v183 — turn types.
--
-- A turn type is a user-approved profile for one kind of turn: what a turn
-- of that kind reads, holds and may touch. The platform understands which
-- type a turn is, in software, and builds the Host's environment from it.
-- Inside a type everything not listed is denied at the tool gate. A thread
-- or project with no type runs exactly as every turn runs today, so this
-- migration changes nothing for a Host whose user has built no type.
--
-- `grant_names` rather than `grant`: GRANT is reserved in SQL. The values
-- are the capability names an agent grant uses (runtime/agent_grant.py);
-- `admin` and `shell_exec` are refused there and never reach a type.
--
-- Nothing writes status / grant_names / keys / bastions in this step. The
-- ticket-decide route becomes the only writer when the `turn_type` tool and
-- its platform-filed card land.

BEGIN;

CREATE TABLE IF NOT EXISTS turn_types (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id           INTEGER NOT NULL
                        REFERENCES entity_registry(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'active', 'retired')),
    -- Words that mean this type, scored like prefire_tools.GROUP_KEYWORDS.
    -- Seeded by the Host at draft time; a dropdown correction adds.
    keywords            TEXT[] NOT NULL DEFAULT '{}',
    -- What the turn reads: {"projects": [..], "stores": [..], "memory": ..}
    context             JSONB NOT NULL DEFAULT '{}'::jsonb,
    skill               TEXT,
    -- Registry `_meta.category` names the turn may use.
    tool_groups         TEXT[] NOT NULL DEFAULT '{}',
    grant_names         TEXT[] NOT NULL DEFAULT '{}',
    -- Credential names any broker door may use; `autofill:<host>` for a
    -- site the autofill door may fill.
    keys                TEXT[] NOT NULL DEFAULT '{}',
    -- Servers any bastion door may request ops on.
    bastions            TEXT[] NOT NULL DEFAULT '{}',
    model               TEXT,
    thread_id           UUID NULL REFERENCES circles_threads(id) ON DELETE SET NULL,
    version             INTEGER NOT NULL DEFAULT 1,
    approved_ticket_id  INTEGER,
    approved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One live name per entity; a retired type may leave its name free.
CREATE UNIQUE INDEX IF NOT EXISTS uq_turn_types_entity_name_live
    ON turn_types (entity_id, lower(name))
    WHERE status <> 'retired';

CREATE INDEX IF NOT EXISTS idx_turn_types_entity_status
    ON turn_types (entity_id, status);

-- Binding: the thread or project that starts a turn carries its type.
-- NULL is "no type" — today's Host, unchanged.
ALTER TABLE circles_threads
    ADD COLUMN IF NOT EXISTS type_id UUID NULL REFERENCES turn_types(id) ON DELETE SET NULL;

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS type_id UUID NULL REFERENCES turn_types(id) ON DELETE SET NULL;

COMMIT;
