-- v162 — the host reads the machines' own records.
--
-- WHAT CHANGES. Until now the oversight ledger folded only what the HEARTBEAT
-- carries: counts per level (v146 `detect`), counts per alert set (v150
-- `alerts`), host posture, and offline. All of those are present tense and all
-- of them are COUNTS — so the ledger could say "three high findings on
-- server-a" and could not say which three, and two machines reporting the
-- same new executable looked like two unrelated numbers.
--
-- The bastion now keeps the findings themselves in a machine store it writes on
-- its own disk (the bastion's machinestore.go), and the host reads
-- that store through `dcn` like any other granted machine store — one governed
-- range-read at a time, against a store the user approved and can revoke. This
-- migration is the two pieces the host side needs: somewhere to remember how
-- far it has read, and permission for the resulting rows to say where they came
-- from.
--
-- A CURSOR AND NOT A TIMESTAMP. The journal is an append-only file per UTC day,
-- so "what is new" is a byte offset into a named file — not a time window. A
-- time window would re-read lines whose clock disagreed with the host's, and
-- skip lines written by a box whose clock ran behind. The offset is exact and
-- it is what the range-read takes anyway (nac_dcn.run_dcn_op, op='read_range').
--
-- ONE ROW PER (store, day). The day is part of the key rather than a column to
-- overwrite, because a box can still be appending to yesterday's file when the
-- host first reads today's — a laptop that woke at 23:58 UTC is the ordinary
-- case, not a corner — and a single moving cursor would strand the tail of
-- yesterday for ever.

DO $$
BEGIN
    IF to_regclass('public.oversight_findings') IS NULL THEN
        RAISE NOTICE 'v162: oversight_findings absent — nothing to extend';
    ELSE
        -- `store` joins the closed provenance vocabulary. It is NOT folded into
        -- 'observe' or 'heartbeat', and the distinction is the point of the
        -- column: `heartbeat` means the box sent a COUNT and this row is the
        -- host's reading of it; `store` means the host read the FINDING itself,
        -- with its evidence, out of the machine's own record. An operator
        -- asking "how do you know" gets a different answer for each, so they
        -- must not share a name.
        ALTER TABLE oversight_findings DROP CONSTRAINT IF EXISTS oversight_findings_source_check;
        ALTER TABLE oversight_findings ADD CONSTRAINT oversight_findings_source_check
            CHECK (source IN ('heartbeat', 'alert', 'observe', 'correlation', 'store'));
    END IF;
END $$;

-- WHICH STORE IS THE MACHINE'S OWN RECORD.
--
-- A user's DCN holds folders they picked. Exactly one machine store per box is
-- different: the bastion's own state dir, holding the record the box keeps
-- about itself. The oversight ingest must read that one and must NOT go
-- trawling the user's documents looking for findings — so the distinction is a
-- column, not a path heuristic. A heuristic on `root` would break the first
-- time somebody names a folder "store", and it would break in the direction
-- where the host reads something it was not meant to.
--
-- NULL for every store a user added, which is the overwhelming majority and the
-- right default: a store says nothing about a purpose unless something set one.
DO $$
BEGIN
    IF to_regclass('public.dcn_stores') IS NULL THEN
        RAISE NOTICE 'v162: dcn_stores absent — nothing to mark';
    ELSE
        ALTER TABLE dcn_stores ADD COLUMN IF NOT EXISTS purpose TEXT;
        ALTER TABLE dcn_stores DROP CONSTRAINT IF EXISTS dcn_stores_purpose_check;
        ALTER TABLE dcn_stores ADD CONSTRAINT dcn_stores_purpose_check
            CHECK (purpose IS NULL OR purpose IN ('machine_record'));
        COMMENT ON COLUMN dcn_stores.purpose IS
            'NULL for a folder the user added. machine_record = the bastion own '
            'state dir, holding the journal the box keeps about itself. '
            'The oversight ingest reads ONLY machine_record stores.';

        -- INSIDE THE GUARD. A bare CREATE INDEX after the DO block is not
        -- covered by the to_regclass check above: on a pod without dcn_stores
        -- the guard returned cleanly and the next statement aborted the
        -- migration under ON_ERROR_STOP=1 — i.e. the host would not come up, in
        -- exactly the case this guard exists for. v146's own comment records
        -- the same bug being made and fixed one migration over; making it again
        -- two files later is the argument for the guard covering everything it
        -- protects rather than just the first statement.
        CREATE INDEX IF NOT EXISTS dcn_stores_machine_record_idx
            ON dcn_stores (entity_id, purpose)
            WHERE purpose IS NOT NULL AND approved_at IS NOT NULL AND revoked_at IS NULL;
    END IF;
END $$;

-- How far the host has read each machine store journal.
--
-- NO FOREIGN KEY TO dcn_stores, deliberately. A revoked store's row must
-- survive: re-granting the same folder later and silently resuming from byte 0
-- would re-open every finding in the retained window as brand new. The cursor
-- is the host's own bookkeeping about a read it performed, not a child of the
-- grant that authorized it.
CREATE TABLE IF NOT EXISTS oversight_store_cursor (
    store_id     BIGINT      NOT NULL,
    -- shelf is 'findings' or 'alerts' — the journal shelves the box writes.
    -- Named rather than assumed, so a later shelf does not silently share a
    -- cursor with this one and skip its lines.
    shelf        TEXT        NOT NULL,
    -- day is the UTC date of the file, exactly as the box names it.
    day          TEXT        NOT NULL,
    -- byte_offset is the FIRST BYTE NOT YET READ. A partial trailing line is
    -- never counted as read (the reader stops at the last newline), so a file
    -- being appended to while it is read cannot lose or split a record.
    byte_offset  BIGINT      NOT NULL DEFAULT 0,
    -- sealed marks a day the box closed (its .capped marker, or the day rolled
    -- past). A sealed day whose offset reached EOF is never read again.
    sealed       BOOLEAN     NOT NULL DEFAULT FALSE,
    last_read_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (store_id, shelf, day)
);

-- The read path: every unfinished day for one store, oldest first, so the
-- journal is folded in the order it was written.
CREATE INDEX IF NOT EXISTS oversight_store_cursor_open_idx
    ON oversight_store_cursor (store_id, shelf, day)
    WHERE sealed = FALSE;

COMMENT ON TABLE oversight_store_cursor IS
    'How far the oversight ingest has read each bastion machine store journal '
    'One row per (store, shelf, UTC day); byte_offset is the first '
    'byte not yet read. Survives a revoke on purpose — resuming a re-granted '
    'store from zero would re-open every retained finding as new.';
