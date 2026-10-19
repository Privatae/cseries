-- ═══════════════════════════════════════════════════════════════════════
-- Migration: entity_tickets + decision_patterns
--
-- These are the entity-private side of "Entity as Endpoint":
--   * tickets — escalations the entity needs the handler to decide on.
--     Each ticket has a snapshot sitrep, a small set of options, and
--     becomes a decision_pattern when the handler picks "and-similar".
--   * decision_patterns — handler-approved rules that let the entity
--     auto-resolve matching future requests without escalating.
--
-- Tickets live in the container's local PG (private to this entity).
-- Decision patterns live in the container's local PG (this entity's
-- learned rules). Circle membership / share scopes live in the platform database.
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS entity_tickets (
    id              SERIAL      PRIMARY KEY,
    ticket_uuid     UUID        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    entity_id       INTEGER     NOT NULL,

    -- Where did this come from?
    --   circle_dispatch — entity escalated from a @mention in a circle
    --   email           — email rule fed the entity a message, it asked
    --   slack | manual  — future ingestion paths
    source          TEXT        NOT NULL,
    source_ref      TEXT,                  -- email msg-id, circle thread, etc.
    circle_id       INTEGER,               -- nullable; tickets without circles go to global queue
    requester       JSONB       NOT NULL DEFAULT '{}'::jsonb,
                                            -- {kind, handle, display_name}

    -- The brief and the options. Sitrep is plain-text dense English.
    -- Options is an array of {id, label, consequence, recommend, pattern_template}.
    sitrep          TEXT        NOT NULL DEFAULT '',
    options         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    context         JSONB       NOT NULL DEFAULT '{}'::jsonb,
                                            -- pulled memory, requirements, attachments

    status          TEXT        NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'decided', 'auto_resolved', 'expired', 'dismissed')),
    priority        SMALLINT    NOT NULL DEFAULT 50,   -- 0..100, higher = sooner

    -- Decision lifecycle
    decision        JSONB,                  -- {option_id, and_similar, note}
    decided_by      INTEGER,
    decided_at      TIMESTAMPTZ,
    pattern_id      INTEGER,                -- FK to decision_patterns when "and-similar"

    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_tickets_entity_status
    ON entity_tickets (entity_id, status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_circle_status
    ON entity_tickets (circle_id, status) WHERE circle_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tickets_source
    ON entity_tickets (entity_id, source, opened_at DESC);


-- ─────────────────────────────────────────────────────────────────────
-- Decision patterns — handler-crystallized rules
--
-- When the handler picks an option WITH "and-similar-going-forward",
-- the entity records the pattern. Future tickets that match the
-- conditions auto-resolve to the same option without paging the
-- handler. The handler sees a digest of auto-resolved tickets in the
-- Reviews queue's "Recent auto-resolved" tab.
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS decision_patterns (
    id              SERIAL      PRIMARY KEY,
    pattern_uuid    UUID        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    entity_id       INTEGER     NOT NULL,
    circle_id       INTEGER,                -- nullable: global-vs-per-circle pattern

    -- What class of decision does this pattern cover?
    -- Free-form so handler/entity can pick natural taxonomies:
    -- "quote_standard_tier", "share_product_specs", "auto_acknowledge".
    action_class    TEXT        NOT NULL,

    -- Match predicates the entity grades incoming tickets against.
    -- Free-form so the entity can encode whatever it inferred from the
    -- approved instance:
    --   {requester_kind: "prospect", ticket_kind: "quote_request",
    --    amount_max_usd: 5000, scope_overlap_required: ["product_specs"]}
    conditions      JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- The decision to apply when conditions match.
    decision_template JSONB     NOT NULL DEFAULT '{}'::jsonb,
                                            -- {option_id, response_template}

    -- Observability + governance
    source_ticket_id  INTEGER,              -- the ticket that crystallized this pattern
    approved_by       INTEGER     NOT NULL,
    approved_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    observations      INTEGER     NOT NULL DEFAULT 1,  -- ticked up on each auto-resolve
    last_used_at      TIMESTAMPTZ,
    status            TEXT        NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'paused', 'retired')),

    CONSTRAINT decision_pattern_unique_per_class
        UNIQUE (entity_id, circle_id, action_class)
);

CREATE INDEX IF NOT EXISTS idx_patterns_entity_status
    ON decision_patterns (entity_id, status, action_class);
CREATE INDEX IF NOT EXISTS idx_patterns_circle
    ON decision_patterns (circle_id) WHERE circle_id IS NOT NULL;

-- After-decided FK so a ticket points at the pattern it spawned.
-- Postgres doesn't support `ADD CONSTRAINT IF NOT EXISTS`, so guard via
-- pg_constraint lookup.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'entity_tickets_pattern_fk'
    ) THEN
        ALTER TABLE entity_tickets
            ADD CONSTRAINT entity_tickets_pattern_fk
            FOREIGN KEY (pattern_id) REFERENCES decision_patterns(id) ON DELETE SET NULL;
    END IF;
END $$;

COMMENT ON TABLE entity_tickets IS
    'Escalations the entity is asking the handler to decide on. Source can be circle dispatch, email, slack, etc. Always private to the entity.';
COMMENT ON TABLE decision_patterns IS
    'Handler-approved rules. When a ticket matches an active pattern, the entity auto-resolves without escalating.';
