-- v218: find a message's rows by the id its sender gave it (the Index's send).
--
-- The Workbench Index sends each message under a request id of the book's own,
-- and the server answers a retry of the same id with the first send's answer
-- (server/routes/workbench_index.py index_send_once). That memory lived in the
-- process, so a retry that came after a restart delivered the words again.
-- Now the id is kept with the delivery: on the marker the landing writes
-- (thread_pick.write_index_marker) and on the user row the words become, the
-- new turn's own or, when they go into a turn already running, the nudge's
-- row (session.py: _send_message_core, and the drain of push_nudge). All
-- three keep it under metadata.client_id, the key a nudge's row has kept its
-- sender's id under, so one lookup reads them (thread_pick
-- .find_index_send).
--
-- A row keeps one id as a JSON string. Words a running turn ended without
-- reading go on as the thread's next turn, several messages joined into one
-- (turn_registry._requeue_undelivered), and that turn's user row keeps all
-- their ids as a JSON array (session.row_client_id). In PII mode a row keeps
-- no id as written: it keeps the digest of each under metadata.client_id_digest
-- instead (mastercode/pii_queue.client_id_digest), in the same two shapes.
--
-- idx_entity_memory_client_id: GIN (jsonb_path_ops) on whichever of the two a
-- row has. The lookup is `entity_id = $1 AND COALESCE(metadata->'client_id',
-- metadata->'client_id_digest') @> to_jsonb($2::text)`: containment of the one
-- id matches a string equal to it and an array that holds it alike. Partial,
-- so a row without an id (nearly all of them) has no entry and costs an
-- insert nothing. The planner uses the index when the query's expression is
-- the one written here, which scripts/test_workbench_index_pg.py holds (custom
-- and generic plans).
--
-- Nothing is backfilled: a row written before this has no client_id, and a send
-- made before it is looked up as one that never landed (it ran under the
-- process that is gone).
--
-- One transaction and no CONCURRENTLY, for the reasons v216 gives: migrations
-- run at boot before the server takes requests, and an interrupted concurrent
-- build leaves an invalid index that IF NOT EXISTS would skip on every later
-- boot. Idempotent (IF NOT EXISTS). Declared for fresh Hosts in init-db.sql
-- too.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_entity_memory_client_id
    ON entity_memory USING gin ((COALESCE(metadata->'client_id', metadata->'client_id_digest')) jsonb_path_ops)
    WHERE COALESCE(metadata->'client_id', metadata->'client_id_digest') IS NOT NULL;

COMMIT;
