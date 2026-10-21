-- v240 (2026-10-19): the owner's passkeys and recovery codes
-- (server/owner_passkeys.py), for a Host the internet reaches.
--
-- A passkey is kept as its credential id, its public key (COSE, as the
-- authenticator gave it) and the authenticator's last signature counter;
-- nothing here signs in by itself. A recovery code is kept only as the
-- SHA-256 of its normalised form, and is spent when used. Once a passkey
-- exists the password no longer signs in (owner_login.sign_in).
--
-- A whole-Host restore keeps the live rows of both tables, not the backup's
-- (server/host_restore.py CARRIED_TABLES): a backup must not bring back a
-- passkey the owner removed or a recovery code already spent.

BEGIN;

CREATE TABLE IF NOT EXISTS owner_passkeys (
    credential_id BYTEA PRIMARY KEY,
    public_key    BYTEA NOT NULL,
    sign_count    BIGINT NOT NULL DEFAULT 0,
    -- What the browser said the authenticator can be reached over (usb, nfc,
    -- ble, internal, hybrid), offered back when the passkey is asked for.
    transports    TEXT[] NOT NULL DEFAULT '{}',
    label         TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS owner_recovery_codes (
    code_hash  TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at    TIMESTAMPTZ
);

COMMIT;

-- The AI's read lane (host_recall, v139) is handed SELECT on every new table
-- by default privileges, and v233, which takes it back from the SECRET tables,
-- runs before this file. These two say who can sign in to this Host, so they
-- are taken back here, where they are created.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'host_recall') THEN
        REVOKE ALL ON public.owner_passkeys FROM host_recall CASCADE;
        REVOKE ALL ON public.owner_recovery_codes FROM host_recall CASCADE;
    END IF;
END $$;
