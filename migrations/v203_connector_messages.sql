-- v203: messaging connectors — the message map.
--
-- One row per messaging-app message the connector route has seen: every
-- message the paired account sent (direction 'in', written by POST
-- /api/connector/messages before the turn starts) and every message the
-- connector sent back for a reply (direction 'out', reported by POST
-- /api/connector/messages/sent). A row says which Host conversation the
-- message belongs to, so a reply to ANY of them in the messaging app, or a
-- message in a platform thread already bound to one, lands there with no
-- pick (4.2 steps 2 and 3).
--
-- The scope is one of the three a connector ever targets (4.2): a Circles
-- thread (thread:<uuid>), a project (project:<id>) or the original Circles
-- conversation (app:circles). The CHECK makes app:<anything else> unwritable:
-- a connector never creates a scope of its own.
--
-- grant_id is the approved grant (nac_connector_grants.id) the connector
-- posted under. A reply is only ever its own grant's: a re-pair of the same
-- app (the old grant revoked, a new one approved, maybe for another account)
-- cannot report sends against, or reattach to, the old grant's replies
-- (message_map.turn_home). Routing (the thread a reply or a platform thread
-- lands in) reads the app's rows whatever the grant: they describe the
-- Host's own threads.
--
-- turn_id is the id the route answered with: the Host turn the connector
-- started (the turn registry's id), or the id of a reply the route wrote itself
-- (a command's answer). It is NULL on an inbound message that was a nudge into
-- a turn the connector did not start. seq is the reply part an outbound
-- message carried. A part may go out as several messages (a text split to the
-- platform's length), so one (turn_id, seq) can have several rows; one
-- platform message is one row per direction (the unique index), which is
-- what makes /sent and a re-delivered inbound message idempotent.
--
-- project_adopt_thread and Circles Split retag these rows with the turns they
-- move (server/connectors/message_map.retag_moved), so a reply to an old
-- message follows the conversation to where it went.
--
-- No message text and no picture is stored here: ids and the scope only.

CREATE TABLE IF NOT EXISTS connector_messages (
    id                   BIGSERIAL PRIMARY KEY,
    entity_id            INT NOT NULL,
    app_id               TEXT NOT NULL,
    grant_id             BIGINT NOT NULL,
    platform             TEXT NOT NULL
                             CHECK (platform IN ('telegram', 'discord', 'slack')),
    chat_id              TEXT NOT NULL,
    external_message_id  TEXT NOT NULL,
    native_thread_id     TEXT,
    scope_kind           TEXT NOT NULL
                             CHECK (scope_kind IN ('thread', 'project', 'app')),
    scope_ref            TEXT NOT NULL,
    direction            TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    turn_id              TEXT,
    seq                  INT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT connector_messages_app_scope_is_circles
        CHECK (scope_kind <> 'app' OR scope_ref = 'circles')
);

-- A reply to a message, and the once-only write of each message.
CREATE UNIQUE INDEX IF NOT EXISTS ux_connector_messages_message
    ON connector_messages (entity_id, app_id, platform, chat_id, external_message_id, direction);
-- A platform thread bound to a Host thread: its newest row.
CREATE INDEX IF NOT EXISTS ix_connector_messages_native_thread
    ON connector_messages (entity_id, app_id, platform, chat_id, native_thread_id, created_at DESC)
    WHERE native_thread_id IS NOT NULL;
-- The thread this chat used last (the pick's "current", the reply's "changed").
CREATE INDEX IF NOT EXISTS ix_connector_messages_chat_recent
    ON connector_messages (entity_id, app_id, platform, chat_id, created_at DESC);
-- A reply's rows: which parts were sent (reattach skips them), and where it ran.
CREATE INDEX IF NOT EXISTS ix_connector_messages_turn
    ON connector_messages (entity_id, app_id, grant_id, turn_id, seq)
    WHERE turn_id IS NOT NULL;
-- The retag after a split or an adopt moves a conversation's turns.
CREATE INDEX IF NOT EXISTS ix_connector_messages_scope
    ON connector_messages (entity_id, scope_kind, scope_ref, created_at);
