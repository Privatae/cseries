-- Migration v151: declare the 1024d HNSW index in a migration, not only in
-- init-db.sql.
--
-- NUMBERED 151: v148, v149 and v150 are taken. Two files claiming one version
-- is not a cosmetic tie: entrypoint.sh globs migrations/v*.sql, applies
-- whichever sorts first for a version and SKIPS the rest, silently, on every
-- boot. scripts/test_migration_versions.py catches it and names that
-- mechanism; check the highest version present when a migration lands.
--
-- WHY.
-- idx_entity_memory_embedding_hnsw covers `embedding`, the 1024d default
-- content column (memory_engine.EMBEDDING_COLUMN_BY_DIM:44). It was declared in
-- exactly one place in the whole tree — init-db.sql:233 — and init-db.sql runs
-- only on a host whose database is empty at boot. So its population was "hosts
-- whose init-db.sql carried line 233 when they were built", which is not a
-- property anything guarantees. Its three siblings are declared by migrations
-- and therefore reach every host by construction: _768_ (v044:40), _2048_
-- (v071:35), _2560_ (v070:34).
--
-- That asymmetry is exactly what retiring runtime DDL is about. The retired
-- ensure_embedding_index() would have built this index lazily the first time a
-- dim column was used; it had no callers and never ran, so deleting it changed
-- nothing — but it did leave the 1024d index as the one member of the set whose
-- declaration is not reachable by construction. This closes that.
--
-- MEASURED BEFORE WRITING THIS: every live host carried the index already
-- (pg_indexes survey — 8 hosts, which was all of them at that moment), so
-- this is a metadata no-op on every host in the fleet
-- today and on every host built from init-db.sql.
--
-- ONE SIBLING IS DELIBERATELY NOT TREATED THE SAME WAY. The 768d index is also
-- declared in two places, and unlike this one the two declarations DIFFER —
-- full in init-db.sql (search idx_entity_memory_embedding_768_hnsw; the line
-- number has moved three times), partial in v044:40. It is left alone, with the
-- measurement written beside it in init-db.sql: the recall arm carries the
-- partial index's own predicate, so both shapes are used by the planner, and
-- converging them would mean a DROP + rebuild on a live host to fix nothing.
-- It fires only on a host that somehow lacks it, which is the case that had no
-- path back. The largest entity_memory in the fleet is 2,874 rows,
-- so even a real build here is instant.
--
-- The definition below is SEMANTICALLY identical to init-db.sql:233, not
-- character-for-character: this one adds IF NOT EXISTS, wraps across three
-- lines, and spaces out the WITH options. Verified rather than asserted — built
-- from each in a container, pg_get_indexdef returns the same string and
-- pg_class.reloptions the same {m=16,ef_construction=64}, so the two
-- populations cannot diverge on definition. If either side changes, re-run that
-- comparison: matching TEXT was never the property that mattered.

BEGIN;

CREATE INDEX IF NOT EXISTS idx_entity_memory_embedding_hnsw
    ON entity_memory USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

COMMIT;
