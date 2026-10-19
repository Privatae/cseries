-- v145: bastion_pairing_tokens gets a declaration at last
--
-- Four modules in server/network/ stop creating their own tables, and exactly
-- ONE of them needed any SQL. That ratio is the whole point of this file, so it
-- is written down before the statement is:
--
--   server/network/pairing_grants.py   3 CREATEs + 1 ALTER   -> NO SQL HERE
--   server/network/machine_map.py      1 CREATE              -> NO SQL HERE
--   server/network/machine_offers.py   1 CREATE + 1 INDEX    -> NO SQL HERE
--   server/network/bastion_enroll.py   1 CREATE              -> the table below
--
-- The first three were defensive MIRRORS of declarations that already shipped.
-- Copying them here would have put a second CREATE of a live table into
-- migrations/, where it reads as authoritative while being free to drift from
-- the real owner. That is not a fix, it is new drift in the clothes of one, so
-- each is named with the file and line that already declares it:
--
--   network_bastion_pairing    migrations/v133_pairing_grants.sql:62
--   nac_pairing_grants         migrations/v133_pairing_grants.sql:90
--     ...and its `models` column  migrations/v140_machine_offers.sql:51
--   network_bastion_other      migrations/v136_bastion_second_bastion_detection.sql:80
--   network_machine_map        migrations/v126_machine_map.sql:19
--   network_machine_offers     migrations/v140_machine_offers.sql:54
--     ...and ux_machine_offers_open  migrations/v140_machine_offers.sql:79
--
-- The `models` ALTER is worth a second look because it is the one statement
-- that was NOT a plain CREATE. pairing_grants.ensure_tables ran
-- `ALTER TABLE nac_pairing_grants ADD COLUMN IF NOT EXISTS models JSONB NOT
-- NULL DEFAULT '[]'` — character for character the statement v140 already
-- carries, against the same table, with the same type and the same default.
-- Deleting it loses nothing: v140 shipped days ago and runs at boot, before any
-- request path could have reached the runtime copy.
--
--
-- bastion_pairing_tokens — DECLARED NOWHERE, and that is a real hole.
--
-- The single-use bearer tokens a bastion redeems to enrol. Until now the only
-- thing that ever created this table was _ensure_pairing_table() in
-- server/network/bastion_enroll.py, called on the mint and redeem paths. Which
-- means a host got the table the first time somebody paired a machine, and not
-- before — so it is live on 4 of 8 hosts (102, 122, 128, 3) and absent on the
-- other 4 (121, 123, 130, 55), exactly matching who has paired something.
-- Deleting that helper with no declaration behind it would leave the four
-- unpaired hosts unable to mint a token at all.
--
-- The shape below is the LIVE shape, and there was nothing to choose between:
-- all four hosts that carry the table carry the same five columns, in the same
-- order, with the same types, nullability and defaults, and the same single
-- primary-key index on secret_hash. No variant, no drift, nothing for this
-- migration to reconcile. It is therefore a pure no-op on those four — the
-- rows on one Host (130 of them) are untouched — and it only does work on a
-- host that has never enrolled a bastion.
--
-- Nothing here depends on a column's current type or on the data in a table, so
-- there is no statement that can abort a boot under entrypoint.sh's
-- ON_ERROR_STOP=1. Idempotent and forward-only, per migrations/README.md.

BEGIN;

CREATE TABLE IF NOT EXISTS bastion_pairing_tokens (
    -- sha256 of the plaintext bearer. The plaintext is returned to the operator
    -- once at mint and never stored, so the hash is the identity of the token
    -- and is what the PK is on.
    secret_hash BYTEA       PRIMARY KEY,
    -- The operator's label for the machine this token is for. Optional: a token
    -- minted for a peer offer has nobody to name it.
    name        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- NOT NULL on purpose, and it is the only bound that always holds: a token
    -- is good for 15 minutes whether or not anything ever consumes it.
    expires_at  TIMESTAMPTZ NOT NULL,
    -- The single-use mark. redeem() consumes the token with one PK-keyed UPDATE
    -- guarded by `used_at IS NULL`, so of two racing redeems the loser matches
    -- zero rows. NULL = unspent.
    used_at     TIMESTAMPTZ
);

COMMIT;
