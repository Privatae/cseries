-- v003: behavioral_corrections.origin
--
-- Adds a TEXT 'origin' column so external-source corrections (karpathy
-- 4-principles, superpowers methodologies, etc.) can be tagged at
-- insert-time and removed reversibly if the upstream changes or is
-- abandoned.
--
-- Origin format convention:
--   origin = 'user'                 — entity owner / operator wrote it
--   origin = 'self_correction'      — entity emitted a correction in-context
--   origin = 'external:karpathy'    — pulled from forrestchang/andrej-karpathy-skills
--   origin = 'external:superpowers' — pulled from obra/superpowers
--   origin = 'external:ecc'         — pulled from affaan-m/everything-claude-code
--
-- Nullable, no default — existing rows stay NULL (=> ad-hoc origin).
-- Indexed for fast filter on selective removal.

ALTER TABLE behavioral_corrections
    ADD COLUMN IF NOT EXISTS origin TEXT;

CREATE INDEX IF NOT EXISTS idx_behavioral_corrections_origin
    ON behavioral_corrections (origin) WHERE origin IS NOT NULL;
