-- v222 (2026-10-11): words the person typed into a running turn, held by the
-- server from the moment it receives them until a turn has them.
--
-- Seen 2026-10-11: a reply a person typed in Circles while their Host
-- was mid-turn vanished when the turn ended. No chat_events 'received' row and
-- no user turn row exist for it. The words had gone to POST /api/circles/nudge,
-- which queued them in process memory only (session.push_nudge); the turn ended
-- without reading them, and the server dropped them because that route's client
-- was trusted to send them again from page memory, which a reload, a closed tab
-- or a dropped connection loses. Words the server did keep for the thread's
-- next turn (session._unread_for_next_turn) were process memory too, so a
-- restart lost those.
--
-- One row per message, written before the route answers (mastercode/
-- pending_words.hold). A row lives exactly as long as no turn has the words:
--   - the running turn reads them at a round boundary -> the row goes
--     (session.py's drain), and the turn writes them as its user row;
--   - the turn ends without reading them -> they start the thread's next turn
--     (turn_registry._requeue_undelivered), and the row goes once that turn
--     has started (its session frame), so a turn that dies before it starts
--     leaves them here;
--   - the Host restarts with rows left -> they start a turn in the room they
--     were sent to, once (pending_words.deliver_left, from the lifespan).
-- So "the server holds these words" is "there is a row", and nothing is read
-- twice: whatever takes the words removes the row.
--
-- `room` is where the words go on as a turn of their own ({app_id,
-- project_id, thread_id, history_context}, the turn registry's record of the
-- turn they were typed into); `thread_key` is that room's lock key
-- (turn_registry.compose_thread_key), which Circles reads the page's held
-- words by. `text` is the words after the secret-shape screen every nudge
-- passes (session.nudge_words): a pasted key is never written here. In PII
-- mode no row is written at all (raw text at rest is what that mode forbids,
-- mastercode/pii_queue.py), and the words stay in process memory as before.
--
-- Idempotent (IF NOT EXISTS). Declared for fresh Hosts in init-db.sql too.

BEGIN;

CREATE TABLE IF NOT EXISTS pending_words (
    id           UUID         PRIMARY KEY,
    entity_id    INTEGER      NOT NULL,
    session_id   TEXT         NOT NULL,
    thread_key   TEXT,
    room         JSONB,
    text         TEXT         NOT NULL,
    tags         JSONB,
    client_id    TEXT,
    -- The app that sent each id (pending_words.sender_of): one, or a JSON
    -- list beside a list of ids. A resend matches the same app's id only.
    client_app   TEXT,
    received_at  TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_pending_words_thread
    ON pending_words (entity_id, thread_key, received_at);

COMMIT;
