-- v079 — harness_run: full-transcript persistence for coding-harness runs.
--
-- The harness pod is ephemeral: scale-to-0 destroys its emptyDir volumes, so today
-- only a 2000-char transcript TAIL survives (in the PVC manifest.json) and the
-- harness's native session files (~/.claude etc.) die with the pod. This table
-- persists the FULL run (transcript + metadata) to host Postgres. It complements
-- harness_usage (metrics only) — one harness_run row per (session_id, harness).
--
-- Persist-ALL; a deterministic trim sweep (curated_at IS NULL) later keeps the
-- high-value runs and archives/strips the junk via status/importance thresholds —
-- modeled on memory_consolidation._decay_old, NO LLM judge.
-- Idempotent, forward-only. Part of the Workbench convergence.
BEGIN;

CREATE TABLE IF NOT EXISTS harness_run (
    id                BIGSERIAL PRIMARY KEY,
    session_id        TEXT,
    project_id        INTEGER,
    harness           TEXT NOT NULL,
    model             TEXT,
    ok                BOOLEAN NOT NULL DEFAULT false,
    exit_code         INTEGER,
    error             TEXT,
    transcript        TEXT,                                   -- FULL, not the 2000-char tail
    diff_bytes        INTEGER NOT NULL DEFAULT 0,
    elapsed_seconds   DOUBLE PRECISION,
    compute_s         DOUBLE PRECISION,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    status            TEXT NOT NULL DEFAULT 'active',          -- active|archived|suppressed (trim lifecycle)
    importance        DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    curated_at        TIMESTAMPTZ,                             -- NULL = not yet swept by the trim
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_harness_run_session   ON harness_run (session_id);
CREATE INDEX IF NOT EXISTS idx_harness_run_recent    ON harness_run (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_harness_run_uncurated ON harness_run (created_at) WHERE curated_at IS NULL;

COMMIT;
