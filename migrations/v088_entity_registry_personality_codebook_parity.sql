-- v088 — schema parity for pre-existing pods (2026-07-12 audit finding F2,
-- revised after a drift-vs-broken analysis).
--
-- init-db.sql grew columns/tables WITHOUT paired migrations, so pods
-- provisioned before those additions lack them. What we restore vs. drop:
--
-- * skill_weights (init-db.sql:93): LIVE READERS exist (personality routes
--   in server/routes/brain.py + task_executor.py tool-proficiency injection)
--   and crash without the column. NOTE: no writer exists anywhere yet — the
--   value stays '{}' until the skill-weights producer is built. Added for
--   reader parity.
--
-- * codebook_recommendations (init-db.sql:937): the CONSUMER half of the
--   governed codebook-update flow is fully built (list/apply/reject services
--   + /api/brain/codebook/* + the codebook_review tool) and crashes without
--   the table. NOTE: the PRODUCER (whatever inserts recommendations from
--   accumulated corrections) is not built yet — the queue lists empty until
--   it is. Added so the shipped consumer works and the producer has its
--   table when it lands.
--
-- * brain_loop_mode: DELIBERATELY NOT ADDED. Zero writers in the tree —
--   vestige of the retired UnifiedBrain loop. The two leftover reads in
--   brain.py were removed instead (same change set).
--
-- Genesis DDL copied verbatim with idempotent guards. No data changes.

ALTER TABLE entity_registry
    ADD COLUMN IF NOT EXISTS skill_weights JSONB DEFAULT '{}';

CREATE TABLE IF NOT EXISTS codebook_recommendations (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entity_registry(id) ON DELETE CASCADE,
    status          VARCHAR(20) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected','expired')),
    proposed_deltas JSONB NOT NULL,
    current_weights JSONB NOT NULL,
    proposed_weights JSONB NOT NULL,
    correction_ids  INTEGER[] NOT NULL DEFAULT '{}',
    correction_count INTEGER NOT NULL DEFAULT 0,
    rationale       TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    resolved_by     VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_codebook_rec_entity
    ON codebook_recommendations (entity_id, status);
