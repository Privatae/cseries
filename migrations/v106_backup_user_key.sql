-- v106_backup_user_key.sql
--
-- The user's OWN backup key, and the management plane's
-- report of what it did with it.
--
-- User backups move to USER-HELD keys with escrow on
-- by default. The per-entity restic repo password goes
-- into a multi-recipient vault envelope on the platform side
-- (privatae migration 044_entity_backup_key). What was missing was the user
-- half: no user had a key, and nothing in the product could make one. These
-- two tables are that half.
--
-- WHY THE PRIVATE KEY IS NOT SEALED TO THE HOST KEY. The nightly job takes an
-- UNFILTERED `pg_dump -Fc -Z0` of this database
-- and ships it to the entity's restic
-- repo. That dump contains kernel_secrets, whose `secret` column is plain
-- BYTEA, and the host's own X25519 private key lives there as
-- `vault_host_x25519` (server/vault/identity.py). So a key sealed to the HOST
-- key is, inside a snapshot, sealed to a key sitting in the same snapshot —
-- and "user-held" would be a slogan.
--
-- Instead the private key is sealed to two recipients DERIVED from things the
-- platform never stores: the user's passphrase and a one-time recovery code
-- (scrypt -> X25519). Same two-source shape as the bastion lane's
-- local_backup_envelope, same envelope format as the vault
-- (server/vault/crypto.py). A dump carries the envelope and the salts; neither
-- opens without the user.
--
-- Mirrors the ensure-schema in server/backup_key.py — the runtime creates
-- these on first use the way the vault does, and this file is the record for a
-- database built from migrations.

CREATE TABLE IF NOT EXISTS backup_user_key (
  id            BIGSERIAL   PRIMARY KEY,
  -- vault identity this key belongs to: 'user:<entity_id>'. The PUBLIC half is
  -- also written to vault_identity_pubkey, which is the row the management
  -- plane's `backup-entity-keys.py sync-user-keys` reads.
  identity      TEXT        NOT NULL,
  pubkey        BYTEA       NOT NULL,
  -- The private key, sealed to the passphrase- and recovery-code-derived
  -- recipients. NULL when the user generated the keypair on their own machine
  -- and registered only the public half — then there is nothing here to hold.
  sealed_priv   JSONB,
  -- scrypt work factors used for THIS row's recipients. Recorded per row so a
  -- later increase cannot orphan an envelope sealed under the old numbers.
  kdf           JSONB,
  origin        TEXT        NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Superseded rows are KEPT. A snapshot taken before a re-key is still sealed
  -- to the OLD key; dropping the row would strand it.
  superseded_at TIMESTAMPTZ,
  CONSTRAINT backup_user_key_pubkey_len CHECK (octet_length(pubkey) = 32),
  CONSTRAINT backup_user_key_origin CHECK (origin IN ('in-pod', 'self-generated'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_backup_user_key_active
  ON backup_user_key (identity) WHERE superseded_at IS NULL;

COMMENT ON TABLE backup_user_key IS
  'The user''s backup X25519 keypair: public key plus the private key sealed to passphrase- and recovery-code-derived recipients. No host recipient, on purpose — the host key rides in the same pg_dump.';

-- What the MANAGEMENT PLANE has actually done with that key. Written by
-- the management plane's key-custody sync on each run.
--
-- Without this the Backup tab can only report that a key was registered HERE.
-- Whether the nightly job re-sealed the repo password to it lives in
-- privatae.entity_backup_key on the platform database, which an entity pod
-- cannot reach — and "your backup is sealed to your key" is exactly the kind of
-- claim that must be a fact rather than a hope.
CREATE TABLE IF NOT EXISTS backup_key_platform_state (
  entity_id   INTEGER     PRIMARY KEY,
  -- recipient fingerprints from entity_backup_key.sealed_to, verbatim
  sealed_to   JSONB,
  user_fp     TEXT,
  escrow      BOOLEAN,
  sealed_at   TIMESTAMPTZ,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE backup_key_platform_state IS
  'Management-plane report of this entity''s backup custody (which recipients the repo password is sealed to, and whether escrow is on). Read-only from the pod''s point of view; absent means "not reported yet", which is not the same as "not sealed to you".';
