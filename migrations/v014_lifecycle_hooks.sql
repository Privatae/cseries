-- v014: lifecycle_hooks — per-entity hook registrations
--
-- Audit Phase 4. Middleware around runtime stage boundaries. ECC-style
-- pre/post hooks: a row here registers a handler to fire at a named
-- stage for a specific entity.
--
-- Four stages in v1 (the "obvious 4"):
--   pre_inference     — before model call; can block
--   post_inference    — after model call; observes (v1: cannot modify)
--   pre_tool_call     — before tool dispatch; can block
--   post_tool_call    — after tool result; observes (v1: cannot modify)
--
-- handler_kind='python' only in v1. handler_ref names a callable in the
-- in-image registry (`mastercode.runtime.lifecycle_handlers`). Future
-- kinds: 'context_store_skill', 'mcp'.
--
-- Block authority: can_block honored at runtime ONLY when origin='builtin'
-- (audited at code-review time). User-installed blockers (origin LIKE
-- 'entity:%' or 'operator') are downgraded to observe-only until a
-- CEIGAS-signed block_authorization column is added in a follow-up.

CREATE TABLE IF NOT EXISTS lifecycle_hooks (
    hook_id         BIGSERIAL    PRIMARY KEY,
    entity_id       INTEGER      NOT NULL,
    name            TEXT         NOT NULL,
    stage           TEXT         NOT NULL,
        -- 'pre_inference' | 'post_inference' | 'pre_tool_call' | 'post_tool_call'
    handler_kind    TEXT         NOT NULL DEFAULT 'python',
    handler_ref     TEXT         NOT NULL,
        -- For kind='python': key in mastercode.runtime.lifecycle_handlers.REGISTRY
    can_block       BOOLEAN      NOT NULL DEFAULT FALSE,
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    priority        INTEGER      NOT NULL DEFAULT 100,
        -- Lower runs earlier within a stage.
    config          JSONB        NOT NULL DEFAULT '{}'::jsonb,
        -- Handler-specific config (e.g. wallet threshold).
    origin          TEXT         NOT NULL DEFAULT 'builtin',
        -- 'builtin' | 'operator' | 'entity:<id>' | 'external:<src>'

    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    UNIQUE (entity_id, name)
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_hooks_stage_enabled
    ON lifecycle_hooks (entity_id, stage, priority) WHERE enabled;
