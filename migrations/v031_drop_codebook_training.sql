-- Migration v030 (Ship MCB5): drop the legacy codebook_training table.
--
-- The table was the parallel-substrate counterpart to entity_memory in
-- the pre-refactor memory model. MCB2 ported all rows to their unified
-- homes (entity_memory + behavioral_corrections + codebook_params)
-- with COPY semantics. MCB3 swapped the engine reader. MCB4+5 retired
-- the writers (skill_events, populate_codebook_training, data_ingestion,
-- entity_genesis clone, oracle_genesis clone, build_competence_codebook
-- rewritten to write codebook_params). domain_classifier legacy fallback
-- removed. Nothing in the live code path references this table after
-- this migration runs.
--
-- Safety: DROP IF EXISTS for idempotency. Existing rows are
-- preserved in entity_memory under metadata.original_content (see v029).

BEGIN;

DROP TABLE IF EXISTS codebook_training CASCADE;

COMMIT;
