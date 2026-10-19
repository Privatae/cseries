-- v020 — PME → EME migration for calendar_events + inbox_messages.
-- Idempotent CREATE IF NOT EXISTS so re-running on a pod that already booted
-- against the updated init-db.sql is a no-op. Indexes mirror central PG.
--
-- Per-entity row backfill is NOT done in this migration. It's done from the
-- central side (a data-only pg_dump of calendar_events and inbox_messages
-- from the platform database, restored on each pod) so we keep the central
-- data immutable while flipping the bridge writers to dispatch to the pod.

BEGIN;

CREATE TABLE IF NOT EXISTS calendar_events (
    id                  SERIAL PRIMARY KEY,
    entity_id           INTEGER NOT NULL,
    title               VARCHAR(500) NOT NULL,
    description         TEXT,
    start_time          TIMESTAMPTZ NOT NULL,
    end_time            TIMESTAMPTZ NOT NULL,
    all_day             BOOLEAN DEFAULT FALSE,
    color               VARCHAR(7),
    recurrence          VARCHAR(50),
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    created_by          VARCHAR(20) DEFAULT 'user',
    category            VARCHAR(50) DEFAULT 'personal',
    event_type          TEXT NOT NULL,
    event_at            TIMESTAMPTZ NOT NULL,
    summary             TEXT NOT NULL,
    payload             JSONB DEFAULT '{}'::jsonb,
    source              TEXT NOT NULL,
    status              TEXT NOT NULL,
    cost_usd            NUMERIC(12,6),
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    model_id            TEXT,
    related_message_id  UUID,
    related_artifact_uri TEXT,
    related_project_id  INTEGER,
    ceigas_crossing_id  UUID,
    scheduled_by        TEXT,
    cancelled_reason    TEXT,
    event_id            UUID DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT calendar_events_event_type_format
        CHECK (event_type ~ '^[a-z][a-z0-9_]*$'),
    CONSTRAINT calendar_events_status_check
        CHECK (status IN ('scheduled','running','completed','failed','cancelled'))
);
CREATE INDEX IF NOT EXISTS idx_calendar_daily_summary
    ON calendar_events (entity_id, event_at) WHERE event_type = 'daily_summary';
CREATE INDEX IF NOT EXISTS idx_calendar_entity_time
    ON calendar_events (entity_id, event_at DESC);
CREATE INDEX IF NOT EXISTS idx_calendar_entity_type
    ON calendar_events (entity_id, event_type, event_at DESC);
CREATE INDEX IF NOT EXISTS idx_calendar_events_category
    ON calendar_events (entity_id, category);
CREATE INDEX IF NOT EXISTS idx_calendar_events_entity_time
    ON calendar_events (entity_id, start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_calendar_scheduled
    ON calendar_events (event_at) WHERE status = 'scheduled';

CREATE TABLE IF NOT EXISTS inbox_messages (
    message_id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    from_address        TEXT NOT NULL,
    to_address          TEXT NOT NULL,
    cc_addresses        TEXT[],
    from_entity_id      INTEGER,
    from_user_email     TEXT,
    to_entity_id        INTEGER,
    to_user_email       TEXT,
    subject             TEXT NOT NULL,
    body                TEXT,
    body_format         TEXT DEFAULT 'text/markdown' NOT NULL,
    message_kind        TEXT DEFAULT 'message' NOT NULL,
    payload             JSONB DEFAULT '{}'::jsonb NOT NULL,
    thread_id           UUID DEFAULT gen_random_uuid() NOT NULL,
    in_reply_to         UUID,
    sender_cert_serial  TEXT,
    sender_signature    TEXT,
    sender_verified     BOOLEAN DEFAULT FALSE NOT NULL,
    ceigas_crossing_id  UUID,
    sent_at             TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    delivered_at        TIMESTAMPTZ,
    read_at             TIMESTAMPTZ,
    archived_at         TIMESTAMPTZ,
    attachments         JSONB DEFAULT '[]'::jsonb NOT NULL,
    CONSTRAINT inbox_messages_body_format_check
        CHECK (body_format IN ('text/markdown','text/plain','text/html')),
    CONSTRAINT inbox_messages_from_required
        CHECK (from_entity_id IS NOT NULL OR from_user_email IS NOT NULL),
    CONSTRAINT inbox_messages_to_required
        CHECK (to_entity_id IS NOT NULL OR to_user_email IS NOT NULL),
    CONSTRAINT inbox_messages_message_kind_check
        CHECK (message_kind IN (
            'message','permission_grant','permission_request',
            'endpoint_request','endpoint_grant','endpoint_revoke',
            'software_order','software_status',
            'contract_proposal','contract_accept','contract_reject',
            'system_notification'
        ))
);
CREATE INDEX IF NOT EXISTS idx_inbox_from_entity
    ON inbox_messages (from_entity_id, sent_at DESC) WHERE from_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inbox_kind_unread
    ON inbox_messages (to_entity_id, message_kind) WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_inbox_recipient_entity
    ON inbox_messages (to_entity_id, sent_at DESC) WHERE to_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inbox_recipient_user
    ON inbox_messages (to_user_email, sent_at DESC) WHERE to_user_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inbox_thread
    ON inbox_messages (thread_id, sent_at);

COMMIT;
