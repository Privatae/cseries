-- v193 (2026-09-28): the keys a backup is encrypted to.
--
-- THE RULE: the platform holds a backup OR a key, never
-- both. Every backup the Host takes (server/host_backup.py) is encrypted to
-- EXACTLY these keys, whose private halves the platform never sees:
--   host-recovery  REQUIRED: the Host recovery key, made in the owner's browser
--                  on the account page; the Host pins the first one it is sent
--   bastion        optional: a key one of the owner's bastions keeps
--                  (label = that bastion)
-- One active host-recovery key; one active row per (kind, label) otherwise.
-- Replaced rows stay, because backups taken earlier are encrypted to them.
--
-- backup_user_key held one active "user" key per Host, made in this pod or
-- registered by hand. That kind is retired: an existing row becomes a replaced
-- 'user' row (kept, never a recipient again), and no new one can be written.
-- On 2026-09-28 no reachable Host had one.

ALTER TABLE backup_user_key ADD COLUMN IF NOT EXISTS kind  TEXT;
ALTER TABLE backup_user_key ADD COLUMN IF NOT EXISTS label TEXT NOT NULL DEFAULT '';
UPDATE backup_user_key SET kind = 'user' WHERE kind IS NULL;
UPDATE backup_user_key SET superseded_at = now() WHERE kind = 'user' AND superseded_at IS NULL;
ALTER TABLE backup_user_key ALTER COLUMN kind SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'backup_user_key_kind') THEN
        -- 'user' only as a retired row.
        ALTER TABLE backup_user_key ADD CONSTRAINT backup_user_key_kind
            CHECK (kind IN ('host-recovery', 'bastion')
                   OR (kind = 'user' AND superseded_at IS NOT NULL));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'backup_user_key_label_len') THEN
        ALTER TABLE backup_user_key ADD CONSTRAINT backup_user_key_label_len
            CHECK (char_length(label) <= 80);
    END IF;
    -- The recovery key's public half comes from the owner's browser, relayed
    -- by the platform: a third origin.
    ALTER TABLE backup_user_key DROP CONSTRAINT IF EXISTS backup_user_key_origin;
    ALTER TABLE backup_user_key ADD CONSTRAINT backup_user_key_origin
        CHECK (origin IN ('in-pod', 'self-generated', 'owner-browser'));
END $$;

-- The old index allowed one active row per Host; now one per (kind, label),
-- and one active host-recovery key whatever its label. Created before the old
-- index is dropped, so there is no moment with no uniqueness at all.
CREATE UNIQUE INDEX IF NOT EXISTS idx_backup_user_key_active_kind_label
    ON backup_user_key (identity, kind, label) WHERE superseded_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_backup_user_key_one_recovery
    ON backup_user_key (identity) WHERE superseded_at IS NULL AND kind = 'host-recovery';
DROP INDEX IF EXISTS idx_backup_user_key_active;
