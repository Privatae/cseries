-- v001: codebook_training.entity_id
--
-- mastercode/codebook_engine.py:load() queries
--   SELECT * FROM codebook_training WHERE codebook_id = $1
--   AND (entity_id = $2 OR entity_id IS NULL) ...
--
-- entity_id was added to init-db.sql after an early Host's PG was first
-- provisioned. Its PG dump+restore preserved the old schema. On 2026-05-18
-- EntityRuntime.initialize() crashed on UndefinedColumnError, runtime
-- stayed None, every chat returned 503 "brain runtime unavailable" and
-- the message silently vanished from the UI.
--
-- Nullable INTEGER referencing entity_registry; NULL = platform-wide
-- training that applies to all entities.

-- Ship MCB5: codebook_training table is dropped in v031. New entities
-- (init-db.sql post-MCB5) never create the table. ALTER TABLE IF EXISTS
-- makes this migration a no-op on fresh init while still applying to
-- legacy pods that have the table from before v031 ran.
ALTER TABLE IF EXISTS codebook_training
    ADD COLUMN IF NOT EXISTS entity_id INTEGER REFERENCES entity_registry(id);
