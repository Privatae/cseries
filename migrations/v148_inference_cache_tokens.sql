-- v148: inference_cost_log.cache_read_tokens / cache_write_tokens — the two
-- counts the cost estimate now prices differently (2026-09-15).
--
-- WHY COLUMNS, when the counts are already logged. provider_gateway has written
-- cache_read_tokens and cache_write_tokens into this table's metadata JSONB
-- since the cache telemetry landed (brain/mastercode/provider_gateway.py:292),
-- and that is where every historical row still carries them. What the JSONB
-- cannot do is be summed by a rollup or filtered by an index: the Compute app's
-- estimate lane reads this table with GROUP BY and SUM (server/routes/
-- compute.py:1618-1671), and `sum((metadata->>'cache_read_tokens')::bigint)`
-- over a JSONB column is both a cast-error risk on any row where the key is
-- absent or non-numeric and unindexable. The columns make the cached subset a
-- first-class fact of the row, the same way tokens_in and tokens_out are.
--
-- WHAT CHANGED ABOVE THEM, which is the reason this is worth a migration at
-- all. Until today the estimate priced the WHOLE of tokens_in at the input
-- rate. tokens_in is the lane-normalized TOTAL input and it INCLUDES the cached
-- subset — the Anthropic path adds cache_read + cache_write to input_tokens
-- (provider_gateway.py:1335-1337) and the OpenAI wire already includes
-- cached_tokens in prompt_tokens, normalised to that at :111-181
-- (_normalize_openai_usage) — so every cache hit was charged
-- full freight. Vendors price cache reads far below input: verified live
-- against https://openrouter.ai/api/v1/models on 2026-09-15, anthropic/
-- claude-opus-5 publishes prompt 0.000005 and input_cache_read 0.0000005, one
-- tenth. With hit rates measured around 84% on real hosts the estimate was
-- running well above the vendor's actual bill, which is the wrong foundation
-- for the dollar-denominated per-turn budget this is meant to carry.
--
-- NOT A NEW cost_method. v113 declares exactly four values — list |
-- subscription | free | unpriced — and the Compute app renders those four. A
-- cache-aware 'list' row is still priced from the provider's own published
-- rates, so it is still 'list'. WHICH rates were applied (a published cache
-- rate, or a fallback to the input rate for a provider that publishes none)
-- is recorded in the metadata under "priced", where it distinguishes an exact
-- row from an approximate one without adding a fifth word to a vocabulary two
-- other surfaces already render.
--
-- NOT NULL DEFAULT 0 is honest here in a way it would not be for a price: zero
-- cached tokens is a real, common measurement (every non-caching provider, and
-- every first call of a conversation), and it is what these rows already mean.
--
-- Idempotent and forward-only, per migrations/README.md.
ALTER TABLE inference_cost_log
    ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER NOT NULL DEFAULT 0;

ALTER TABLE inference_cost_log
    ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER NOT NULL DEFAULT 0;

-- BACKFILL, and the reason it is not optional. DEFAULT 0 means "no tokens came
-- from cache", which is a measurement — and it would be a false one on every
-- row written before this migration, because those rows carry their counts in
-- the metadata and nowhere else. The Compute app renders this column as "from
-- cache" (system-apps/compute/app.js, app.yaml 0.2.3), so leaving them at 0
-- would print the same zero-that-looks-like-data this table's own cost_method
-- column exists to prevent. The re-pricing passes read the JSONB fallback
-- either way (server/routes/inference.py:_row_cache_tokens); the READER cannot.
--
-- Cast safety, both halves. The WHERE compares AS JSONB — no cast, so a row
-- holding a non-numeric value there cannot abort the statement. AND does not
-- guarantee left-to-right evaluation in Postgres, so the cast lives in a CASE
-- (which does), guarded by jsonb_typeof; a non-number leaves the column alone.
-- LEAST/GREATEST bound the result into INTEGER — via numeric, so an
-- out-of-range value clamps instead of raising.
--
-- BATCHED, BY id WINDOW, AND THAT IS THE LOAD-BEARING PART. This runs inside
-- entrypoint.sh's migration loop, which BLOCKS THE BOOT: one unbounded
-- sequential UPDATE over this table holds a single transaction open for as long
-- as it takes, and the table has no ceiling — it is one row per model call for
-- the life of the host. The measured hosts are small (~1-2k rows), but "the
-- hosts I looked at are small" is not a bound, and the failure mode if one is
-- not is a host that will not come up. So: a fixed-size window walked by
-- PRIMARY KEY, each batch its own statement, committing as it goes.
--
-- The window is over the id RANGE rather than a LIMIT on matching rows,
-- because a LIMIT would re-scan from the start of the table on every
-- iteration (quadratic on the one table size this exists to protect against).
-- Stepping the range means each batch touches one bounded slice of the primary
-- key exactly once, whether or not any row in it matched.
--
-- Idempotent at both levels: a backfilled row no longer matches `= 0`, a row
-- whose true count IS zero is a no-op anyway, and a re-run walks the same
-- windows finding nothing. Safe to interrupt — a killed boot leaves the
-- batches that committed, and the next boot re-runs the whole migration (this
-- file is only recorded in schema_migrations after it returns) and completes
-- the rest.
DO $backfill$
DECLARE
    batch   CONSTANT bigint := 10000;
    lo      bigint;
    hi      bigint;
    max_id  bigint;
    touched bigint := 0;
    n       bigint;
BEGIN
    SELECT coalesce(min(id), 1), coalesce(max(id), 0)
      INTO lo, max_id FROM inference_cost_log;
    WHILE lo <= max_id LOOP
        hi := lo + batch - 1;

        UPDATE inference_cost_log
           SET cache_read_tokens = CASE
                   WHEN jsonb_typeof(metadata->'cache_read_tokens') = 'number'
                   THEN LEAST(GREATEST((metadata->>'cache_read_tokens')::numeric, 0),
                              2147483647)::int
                   ELSE cache_read_tokens END
         WHERE id BETWEEN lo AND hi
           AND cache_read_tokens = 0
           AND metadata->'cache_read_tokens' > '0'::jsonb;
        GET DIAGNOSTICS n = ROW_COUNT;
        touched := touched + n;

        UPDATE inference_cost_log
           SET cache_write_tokens = CASE
                   WHEN jsonb_typeof(metadata->'cache_write_tokens') = 'number'
                   THEN LEAST(GREATEST((metadata->>'cache_write_tokens')::numeric, 0),
                              2147483647)::int
                   ELSE cache_write_tokens END
         WHERE id BETWEEN lo AND hi
           AND cache_write_tokens = 0
           AND metadata->'cache_write_tokens' > '0'::jsonb;
        GET DIAGNOSTICS n = ROW_COUNT;
        touched := touched + n;

        lo := hi + 1;
        -- COMMIT PER WINDOW is what actually bounds the transaction. A DO block
        -- is otherwise ONE transaction however many statements it runs, so
        -- batching alone would bound each statement's work while still holding
        -- every row lock until the last window finished — most of the problem,
        -- unfixed. Legal because the block runs outside an explicit
        -- transaction: entrypoint.sh applies the file with `psql -f`, which
        -- autocommits per statement. THIS FILE MUST NOT BE WRAPPED IN
        -- BEGIN/COMMIT (v147 wraps its own; this one deliberately does not),
        -- and entrypoint.sh's apply loop must never gain -1/
        -- --single-transaction. Measured on cseries-postgres:smoke 2026-09-15:
        -- unwrapped the block runs, and under EITHER wrapper it is
        -- `ERROR: invalid transaction termination`. Both halves of that are
        -- enforced by scripts/test_migrations_guarded.py, because neither is
        -- visible from this file.
        COMMIT;
    END LOOP;
    RAISE NOTICE 'v148: backfilled cache counts on % row-columns (ids 1..%)',
                 touched, max_id;
END
$backfill$;
