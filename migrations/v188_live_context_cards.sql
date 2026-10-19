-- v188: live context cards the Host builds for the person
-- (brain/mastercode/live_context_host.py).
--
-- A Host builds a card the way it builds an op: files in a project module, here
-- one with target 'live_context', one `<card id>.yaml` per card, written with
-- the tools it already has. The platform validates the card against the app it
-- reads, renders a preview against the real rows, and files a card in the
-- person's Tickets. The card goes live only on the person's one click on that
-- preview — a card puts text into every matching prompt.
--
-- live_* is what serves; draft_* is what waits for the click. A change to a
-- live card is a new draft and the live version keeps serving until the person
-- answers, so a pending edit never takes a working card down.

ALTER TABLE project_modules DROP CONSTRAINT IF EXISTS project_modules_target_check;
ALTER TABLE project_modules ADD CONSTRAINT project_modules_target_check
    CHECK (target IN ('private_app','public_site','store_app','component','op','live_context'));

CREATE TABLE IF NOT EXISTS live_context_cards (
    id                  SERIAL PRIMARY KEY,
    entity_id           INTEGER NOT NULL,
    card_id             TEXT NOT NULL,
    app_id              TEXT NOT NULL,           -- the installed app whose declared data it reads
    project_id          INTEGER,
    module              TEXT,
    status              TEXT NOT NULL CHECK (status IN ('draft', 'live', 'retired')),
    live_spec           JSONB,
    live_sha            TEXT,
    approved_at         TIMESTAMPTZ,
    approved_ticket_id  INTEGER,
    draft_spec          JSONB,
    draft_sha           TEXT,
    draft_ticket_id     INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at          TIMESTAMPTZ,
    UNIQUE (entity_id, card_id)
);

CREATE INDEX IF NOT EXISTS live_context_cards_live
    ON live_context_cards (entity_id) WHERE status = 'live';

-- A turn type may name the cards it carries on every turn of its kind,
-- whatever the message says (mastercode.live_context.type_cards). Written only
-- by the person's click on the type's card, like every other profile field.
ALTER TABLE turn_types ADD COLUMN IF NOT EXISTS cards TEXT[] NOT NULL DEFAULT '{}';
