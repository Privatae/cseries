-- v152 — reach-out: the host reaches its person on a channel they own.
--
-- A finished build / fan-out / delivery / agent writes a durable inbox row
-- (brain/mastercode/runtime/session.py, payload.event ∈ build_finished |
-- fanout_finished | app_proven | agent_finished) and a published site writes
-- one too (server/routes/applications.py, payload.event = site_published).
-- Those rows are the bus. These three tables are the subscription on it.
--
-- WHERE THE DDL LIVES. Here, and nowhere else. The kernel does not create its
-- own schema any more;
-- scripts/test_no_runtime_ddl.py refuses a CREATE/ALTER in server/**/*.py and
-- its allowlist only shrinks. init-db.sql is NOT touched: v146 (a column) and
-- v147 (six new tables) both left it alone, because a migration applied under
-- ON_ERROR_STOP=1 at every boot already produces the table on an empty
-- database — a second declaration is a second thing to keep in step.
--
-- THE RULE THIS MIGRATION OBEYS: only
-- CREATE TABLE IF NOT EXISTS / ADD COLUMN IF NOT EXISTS / CREATE INDEX
-- IF NOT EXISTS. Nothing here depends on a column's current type, so this
-- file is a clean no-op against a database that already has the tables and
-- produces the full shape against an empty one. Both directions verified
-- against cseries-postgres:smoke.
--
-- NOTE ON THE NUMBER: entrypoint.sh keys schema_migrations on the NUMBER
-- alone, so two files with one number would not collide loudly — the first to
-- apply would record it and the second would be SKIPPED in silence, leaving a
-- host whose reach-out tables never existed. scripts/test_migration_versions.py
-- is the gate for that.

-- ── Channels — HOW to reach the user. One row per way. ────────────────────
CREATE TABLE IF NOT EXISTS reachout_channels (
    id           SERIAL PRIMARY KEY,
    entity_id    INTEGER NOT NULL,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    action       JSONB NOT NULL,
    cooldown_s   INTEGER NOT NULL DEFAULT 120,
    quiet_from   TIME NULL,
    quiet_to     TIME NULL,
    quiet_tz     TEXT NULL,
    created_by   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    disabled_at  TIMESTAMPTZ NULL,
    UNIQUE (entity_id, name)
);

COMMENT ON TABLE reachout_channels IS
    'How the host reaches its person. `action` names a transport and its '
    'arguments and NEVER carries a secret — an http_call action carries the '
    'vault NAME of a credential, opened server-side by vault.authz.'
    'use_credential at fire time. cooldown_s defaults to 120 (design decision '
    '3): a burst of finished agents is one nudge, and it is the user''s to '
    'change, never a cap on how many triggers exist.';

COMMENT ON COLUMN reachout_channels.quiet_from IS
    'Opt-in quiet window; NULL by design decision 2 — quiet hours are the '
    'user''s to set and the platform ships none.';

COMMENT ON COLUMN reachout_channels.created_by IS
    'host | user | platform. The host may delete what IT created; a row the '
    'user created is theirs to delete, from Circles.';

-- The dispatcher's hot path: enabled channels for this entity.
CREATE INDEX IF NOT EXISTS reachout_channels_entity_live_idx
    ON reachout_channels (entity_id) WHERE disabled_at IS NULL;

-- ── Triggers — WHEN to reach out. ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reachout_triggers (
    id           SERIAL PRIMARY KEY,
    entity_id    INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL REFERENCES reachout_channels(id) ON DELETE CASCADE,
    label        TEXT NOT NULL,
    match_kind   TEXT NOT NULL,
    match_event  TEXT NULL,
    match_jsonb  JSONB NULL,
    created_by   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    disabled_at  TIMESTAMPTZ NULL
);

COMMENT ON TABLE reachout_triggers IS
    'A subscription on inbox_messages: match_kind is an inbox message_kind, '
    'match_event an optional payload.event, match_jsonb an optional '
    'containment predicate on payload. There is deliberately no column for '
    'another entity''s id — a host subscribes to its own inbox and nothing '
    'else.';

COMMENT ON COLUMN reachout_triggers.match_event IS
    'payload.event. Live today: build_finished, fanout_finished, app_proven, '
    'agent_finished (session.py) and site_published (applications.py). '
    'Declared and NOT YET EMITTED BY ANYTHING: oversight_critical and '
    'oversight_high, which the management lane will write. A trigger on '
    'either is accepted and will simply never fire until that lane emits it.';

CREATE INDEX IF NOT EXISTS reachout_triggers_entity_live_idx
    ON reachout_triggers (entity_id, match_kind) WHERE disabled_at IS NULL;
CREATE INDEX IF NOT EXISTS reachout_triggers_channel_idx
    ON reachout_triggers (channel_id);

-- ── Log — WHAT happened. Every fire is a row. ─────────────────────────────
CREATE TABLE IF NOT EXISTS reachout_log (
    id           BIGSERIAL PRIMARY KEY,
    entity_id    INTEGER NOT NULL,
    trigger_id   INTEGER NULL,
    channel_id   INTEGER NULL,
    message_id   UUID NOT NULL,
    fired_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status       TEXT NOT NULL,
    detail       JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (trigger_id, message_id)
);

COMMENT ON TABLE reachout_log IS
    'One row per ATTEMPT, written before the attempt resolves. The UNIQUE '
    '(trigger_id, message_id) is the idempotency gate: the dispatcher inserts '
    'ON CONFLICT DO NOTHING RETURNING id, so only the tick that wins the '
    'insert fires, and a second tick over the same inbox row is a no-op. That '
    'is also why there is NO RETRY: a failed attempt has already consumed its '
    'row, and a doorbell that rings again later is worse than one that did '
    'not ring.';

COMMENT ON COLUMN reachout_log.status IS
    'pending | sent | skipped_cooldown | skipped_quiet | failed. `pending` is '
    'the row the idempotency gate wrote before the attempt resolved — a row '
    'still reading pending means the pod stopped mid-attempt, which is a '
    'distinct fact from failed and is deliberately kept distinguishable.';

COMMENT ON COLUMN reachout_log.detail IS
    'Redacted outcome: http status, runbook event id, or the error. Never a '
    'secret, never an auth header, and never conversation content — the '
    'rendered text is derived from a closed variable set that has no msg.body '
    'in it (design principle 1).';

CREATE INDEX IF NOT EXISTS reachout_log_entity_fired_idx
    ON reachout_log (entity_id, fired_at DESC);
-- The cooldown read: the last successful send on one channel.
CREATE INDEX IF NOT EXISTS reachout_log_channel_sent_idx
    ON reachout_log (channel_id, fired_at DESC) WHERE status = 'sent';
