-- v192 (2026-09-28): the Host backs itself up, encrypted to keys the platform
-- does not hold.
--
-- Backups are a Host op; each is encrypted on the Host to the
-- owner's Host recovery key and their bastions' keys (server/host_backup.py,
-- server/backup_key.py). What used to be the management plane's bookkeeping
-- lives here, next to the data.

-- One row per run: when it ran, why, where the encrypted file went, and how it
-- ended. The Settings Backup tab and the second copy (server/backup_ship.py)
-- read the latest rows; nothing is ever updated after a run finishes except by
-- that run.
CREATE TABLE IF NOT EXISTS host_backup_runs (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    reason          TEXT NOT NULL,        -- schedule | catch-up | quiet | manual | before-restore | restore
    state           TEXT NOT NULL,        -- running | ok | partial | failed | no_recipient
    stamp           TEXT,                 -- YYYYmmddTHHMMSSZ, the object's name
    stored          TEXT,                 -- backup (platform storage) | staged (for a second copy)
    object_key      TEXT,                 -- the encrypted file, as the platform named it
    sidecar_key     TEXT,                 -- its sealed per-backup key
    size_bytes      BIGINT,
    sha256          TEXT,
    sidecar_sha256  TEXT,
    sidecar_bytes   BIGINT,
    recipients      JSONB NOT NULL DEFAULT '[]'::jsonb,   -- labels only: user, recovery:primary, ...
    parts           JSONB NOT NULL DEFAULT '{}'::jsonb,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_host_backup_runs_started
    ON host_backup_runs (started_at DESC);

-- Small named facts the Host keeps between runs: a restore's progress, a
-- bastion's reseal job, and a recovery key the account page offers in place of
-- the pinned one (public halves only). Never a key that opens a backup:
-- everything here goes into the next backup.
CREATE TABLE IF NOT EXISTS host_backup_state (
    k           TEXT PRIMARY KEY,
    v           JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
