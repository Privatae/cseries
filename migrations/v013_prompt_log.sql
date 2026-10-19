-- v013: prompt_log — provenance for every system-prompt rebuild
--
-- Audit Phase 5 (2026-05-18). One row per call to the schema orchestrator
-- (`prompt_schema._build_grouped`). Captures which blocks fired, how many
-- chars each emitted, and which freshness scope was assembled.
--
-- Lets us inspect "what was actually in the prompt for entity N session S
-- turn T" without retrieving the raw prompt body. Useful for prompt-pipeline
-- regression hunts, cache-effectiveness analysis, and entity-quality work.
--
-- Best-effort write — schema build never blocks on this insert.

CREATE TABLE IF NOT EXISTS prompt_log (
    log_id          BIGSERIAL    PRIMARY KEY,
    entity_id       INTEGER      NOT NULL,
    session_id      TEXT,                                 -- nullable; sessionless rebuilds possible
    scope           TEXT         NOT NULL,                -- 'static+per_rebuild' | 'per_turn' | composite
    block_count     INTEGER      NOT NULL,
    total_chars     INTEGER      NOT NULL,
    has_cache_break BOOLEAN      NOT NULL DEFAULT FALSE,
    blocks          JSONB        NOT NULL DEFAULT '[]'::jsonb,
                    -- [{"name": "identity", "chars": 1234, "freshness": "per_rebuild"}, ...]

    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_prompt_log_entity_time
    ON prompt_log (entity_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_prompt_log_session
    ON prompt_log (session_id, created_at DESC) WHERE session_id IS NOT NULL;
