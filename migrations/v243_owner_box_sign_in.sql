-- v243: the owner of a C-Series Cloud Host, made and signed in from the
-- machine's unlock page (server/owner_login.py claim_owner, box_sign_in).
--
-- That owner has no password: holding one of the disk's keys is what signs
-- them in, through a one-time ticket the machine makes and the Host checks.
-- So password_hash may be empty. box_ticket_hash is the SHA-256 of the
-- newest such ticket the Host took, so each ticket signs in once.

BEGIN;

ALTER TABLE owner_account ALTER COLUMN password_hash DROP NOT NULL;
ALTER TABLE owner_account ADD COLUMN IF NOT EXISTS box_ticket_hash TEXT;

COMMIT;
