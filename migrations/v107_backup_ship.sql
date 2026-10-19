-- v107_backup_ship.sql
--
-- Shipping the user's backup to a destination THEY name.
--
-- The POD ships, to any S3-compatible endpoint the user
-- names, with the credential named by target.cred_name opened in-pod — the
-- platform never holds it. `nfs` and `smb` are replaced by one kind, "my own
-- machine": the artifact is delivered to the user's bastion, which writes it
-- into a directory they configure (a NAS mount, if that is what they point it
-- at).
--
-- WHAT GETS SHIPPED, and why it is not the restic repo. The source is the
-- export artifact: one tar.zst with a manifest inside, age-encrypted to
-- the user's own recipients BEFORE it leaves the management plane
-- (ops/backup-custody/backup-export.py). Shipping that to a third party leaks
-- nothing, because it is already sealed to keys the platform does not hold.
-- Shipping the restic repo instead would mean this pod holding the repo
-- password, which is the one thing the custody work exists to avoid.
--
-- WHY A SOURCE TABLE. The export lives in account_exports on the PLATFORM
-- database, which an entity pod cannot reach. The management plane already
-- reaches into the pod once a sync to report custody
-- (backup_key_platform_state, v106); it publishes the ready export the same
-- way, and the pod's ship worker reads it here. One channel, already built,
-- already audited.

CREATE TABLE IF NOT EXISTS backup_ship_source (
  -- account_exports.id on the platform side. The natural key: one row per
  -- export, and re-publishing the same export is an upsert rather than a
  -- second thing to ship.
  export_id      BIGINT      PRIMARY KEY,
  s3_key         TEXT        NOT NULL,
  -- Short-lived signed GET. Re-published by the management plane as it
  -- re-signs (backup-export.py re-signs anything with < 1h left), so an
  -- expired URL is a transient state, not a dead end.
  url            TEXT,
  url_expires_at TIMESTAMPTZ,
  size_bytes     BIGINT      NOT NULL,
  sha256         TEXT        NOT NULL,
  snapshot_time  TIMESTAMPTZ,
  published_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT backup_ship_source_sha_len CHECK (char_length(sha256) = 64)
);

COMMENT ON TABLE backup_ship_source IS
  'The newest ready export artifact for this entity, published into the pod by ops/backup-custody/backup-entity-keys.py. Already encrypted to the user''s own recipients; the URL is short-lived and re-published as it is re-signed.';

-- One row per attempt. Kept, not overwritten: "it worked last night and has
-- failed every night since" is the shape of the problem a user needs to see,
-- and a single status column cannot show it.
CREATE TABLE IF NOT EXISTS backup_ship_log (
  id           BIGSERIAL   PRIMARY KEY,
  export_id    BIGINT      NOT NULL,
  kind         TEXT        NOT NULL,
  -- Endpoint/bucket for s3, the bastion's name for machine. Never a credential:
  -- this row lands in every pg_dump, same rule as the target itself.
  destination  TEXT        NOT NULL,
  state        TEXT        NOT NULL,
  bytes_sent   BIGINT      NOT NULL DEFAULT 0,
  -- The CLASS of failure, from a closed set the UI can explain, so the tab
  -- never has to render a raw exception at a user.
  reason       TEXT,
  -- The underlying message, verbatim and truncated. A user chasing a bucket
  -- policy needs the provider's own words, not our paraphrase.
  detail       TEXT,
  started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Bumped while a transfer is demonstrably still moving. The in-flight lock is
  -- reaped on SILENCE, not on age: a bastion fetch may legitimately run for
  -- hours, and reaping it on total age would kill a healthy transfer and start
  -- a second download of the same artifact over the user's home connection.
  heartbeat_at TIMESTAMPTZ,
  finished_at  TIMESTAMPTZ,
  CONSTRAINT backup_ship_log_kind CHECK (kind IN ('s3', 'machine')),
  CONSTRAINT backup_ship_log_state CHECK (state IN ('running', 'done', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_backup_ship_log_recent
  ON backup_ship_log (started_at DESC);
-- At most one attempt in flight, fleet-wide for this entity. A second ship
-- while the first is streaming would double the egress and race on the
-- destination key. A partial unique index on the state value itself is the
-- plain way to say it: 'running' can appear at most once.
CREATE UNIQUE INDEX IF NOT EXISTS idx_backup_ship_log_running
  ON backup_ship_log (state) WHERE state = 'running';

COMMENT ON TABLE backup_ship_log IS
  'One row per attempt to ship a backup artifact to the user''s own destination. Kept for history: a lane that worked once and has failed since is the shape a single status column cannot show.';
