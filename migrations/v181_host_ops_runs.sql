-- v181: host-authored ops (bastion 0.37.0; server/network/nac_ops.py).
--
-- A machine runs ops. An op is a module with target `op` (the fifth module
-- target: a program the Host runs on the user's own machines through their
-- bastion, never an app), one directory per op, identified by the sha256 of
-- its canonical form. It reaches a machine only under terms this Host signed
-- and the box verifies: `op_run` (once — a dry run in the machine's sandbox,
-- the person's approval bound to the change list it showed, the apply) or
-- `op_grant` (from now on — bounds, a use count, user only).
--
-- host_op_runs is the Host's own record of each: the dry run's report (what
-- the card shows and what the apply is bound to), the terms' nonce and expiry
-- (so a pending approval is asked for under ONE purpose across calls), the
-- approval spent, the box's answer. The box keeps its own record per run
-- (ops/runs/<id>.json) and the audit line; this is the Host's side.

ALTER TABLE project_modules DROP CONSTRAINT IF EXISTS project_modules_target_check;
ALTER TABLE project_modules ADD CONSTRAINT project_modules_target_check
    CHECK (target IN ('private_app','public_site','store_app','component','op'));

CREATE TABLE IF NOT EXISTS host_op_runs (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL,
    server          TEXT NOT NULL,
    token_id        INTEGER,
    kind            TEXT NOT NULL CHECK (kind IN ('op_run','op_grant')),
    op              TEXT NOT NULL,
    version         TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    privilege       TEXT NOT NULL,
    params          TEXT,                    -- op_run: the canonical params text, as signed
    bounds          TEXT,                    -- op_grant: the canonical bounds text, as signed
    uses            INTEGER,                 -- op_grant
    nonce           TEXT NOT NULL UNIQUE,
    expires         TIMESTAMPTZ NOT NULL,
    changes_sha256  TEXT,                    -- op_run: the change list the dry run showed
    report          JSONB,                   -- the dry run's report (changes, output)
    state           TEXT NOT NULL
                    -- sent: the apply left for the box and its answer was lost (a
                    -- second apply of the same run is refused there; see nac_ops).
                    CHECK (state IN ('dry_run','awaiting','sent','applied','refused','installed','dropped')),
    approval        TEXT,                    -- the approval spent (device:<nonce> | console:<id> shape)
    result          JSONB,                   -- the box's answer to the apply / the install
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS host_op_runs_lookup
    ON host_op_runs (entity_id, server, sha256, created_at DESC);
