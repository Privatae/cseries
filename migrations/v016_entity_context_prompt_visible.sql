-- v016: entity_context.prompt_visible — gate which files surface in prompts.
--
-- Per Codebook Governance: the user approves what
-- shows up in the entity's system prompt. The `ContextStore.skills/`
-- prefix was previously uncurated — any file under `skills/` showed up
-- in the prompt's "What You Know How To Do" block. The `manage_tasks.md`
-- file that contradicted the no-autonomous-loops rule had to be deleted
-- by hand because there was no governance gate.
--
-- This adds a `prompt_visible` column that defaults to FALSE for newly-
-- inserted rows. Existing rows are backfilled to TRUE so this change
-- doesn't silently drop content the entity is already seeing. New
-- skills/ files (added by the entity, by tooling, by external seeds)
-- require explicit approval before they surface.
--
-- Approval mechanism (today): direct UPDATE by the operator. A small
-- admin endpoint can come later; the data shape supports it.

ALTER TABLE entity_context
    ADD COLUMN IF NOT EXISTS prompt_visible BOOLEAN NOT NULL DEFAULT FALSE;

-- Backfill: preserve current behaviour for files already in the store.
-- After this migration:
--   - existing rows (any prefix) keep their current visibility
--   - new rows default to FALSE until an operator marks them TRUE
UPDATE entity_context SET prompt_visible = TRUE WHERE prompt_visible = FALSE;

CREATE INDEX IF NOT EXISTS idx_entity_context_prompt_visible
    ON entity_context (entity_id, file_path) WHERE prompt_visible;
