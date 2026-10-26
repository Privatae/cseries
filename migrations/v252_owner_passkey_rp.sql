-- v252 (2026-10-25): which address each of the owner's passkeys works on.
--
-- A member Host with remote access answers on two addresses: the desktop's
-- own (CSERIES_URL) and the remote one (CSERIES_REMOTE_URL,
-- server/home_origin.py). A passkey's relying party is the address it was
-- made on, exactly, so a passkey made on a phone on the remote address works
-- there and nowhere else (server/owner_passkeys.py). rp_id is that address's
-- host name; '' is a passkey kept before there were two, which is the
-- desktop's own.
--
-- Additive and idempotent; forward-only.

BEGIN;

ALTER TABLE owner_passkeys ADD COLUMN IF NOT EXISTS rp_id TEXT NOT NULL DEFAULT '';

COMMIT;
