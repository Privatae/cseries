-- v138 — drop the table a retirement left behind on existing hosts
-- (scored_outcomes).
--
-- WHY A MIGRATION AND NOT JUST A SCHEMA EDIT. Removing a CREATE TABLE only
-- affects hosts built AFTER the removal. Every host that already ran the old
-- schema keeps the table forever, with its rows, its indexes and its share of
-- the host's fixed storage allocation — and it becomes invisible to
-- scripts/audit_orphan_tables.py at the same moment, because that linter
-- computes `declared - referenced` and a removed CREATE also removes the name
-- from `declared`. This file is the other half of a retirement. The linter now
-- enforces that other half: see the retirement ratchet in
-- audit_orphan_tables.py and scripts/declared_tables_baseline.txt.
--
-- ─────────────────────────────────────────────────────────────────────────
-- signal_queue IS NOT DROPPED HERE, AND MUST NOT BE.
--
-- The first cut of this file dropped it, on the reasoning that the
-- services/awareness/ intake pipeline was retired (init-db.sql:1154-1161), the
-- writer code is gone from the tree, and the rows have been frozen since
-- So they were "abandoned intake" and nothing was stuck on them.
--
-- Every one of those facts is true and the conclusion drawn from them was
-- wrong. What the table IS was never established, only what stopped running.
-- Its shape says it plainly in hindsight: an `embedding` column and a
-- `promoted_memory_ids` column, and rows moving pending -> summarized/judged.
-- It is a staging table whose output is entity_memory, with a vector, which is
-- to say a SEARCHABLE CACHE OF WHAT THE HOST HAS READ.
--
-- The intent, which the schema corroborates:
--   * research provenance — being able to answer "how did you reach that?"
--     with "we discussed it on this date, from this source", which is the
--     thing models are famously unable to do;
--   * long-term knowledge — research the Host has read becomes what the Host
--     KNOWS, rather than something it re-fetches;
--   * continuity.
--
-- So this is a feature that was half-removed, not residue. 2,130 of the 2,229
-- rows on one long-lived Host are research; 167 of them reached `summarized` and became
-- memories, which is the mechanism working. The rest are what was in flight
-- when the consumer was deleted.
--
-- The retirement note's claim that "signal() now routes through
-- MemoryEngine.extract_and_store" holds for exactly ONE source — external chat
-- turns, via server/synaptive_handler.py:838. The research source has no
-- replacement: _exec_web_search and _exec_research (runtime/tools.py:10019,
-- :10038) call runtime/web_research and return the provider's answer to the
-- model, and that module touches the database twice, both reads (an API key
-- and a routing lookup). Nothing writes. Nothing reads memory before searching
-- either, so the property is absent at both ends.
--
-- DO NOT re-add the drop. If the rows are ever genuinely unwanted, that is a
-- decision about a product feature and belongs with whoever owns it, not in a
-- schema-hygiene migration.
-- ─────────────────────────────────────────────────────────────────────────
--
-- MEASURED across all eight running entity pods (to_regclass in each
-- pod's own DB — the per-pod DB is the only place this data exists):
--
--   scored_outcomes  one host         present, 0 rows
--                    all other seven  absent
--
-- scored_outcomes: never declared in this repo at all (no CREATE in init-db.sql
-- or any migration, over the whole git history). Its writer and both readers
-- were deleted in the same change as this file; see the tombstone in
-- brain/mastercode/brain_memory.py. The one existing copy is fork residue with 0 rows
-- and pg_stat_user_tables.n_tup_ins = 0 — nothing ever wrote to it. That is the
-- difference from signal_queue above: this table never worked, rather than
-- having worked and been unplugged.
--
-- IF EXISTS: seven of eight hosts never had it, and a migration that errors
-- there would fail the pod's entrypoint (entrypoint.sh runs every file with
-- ON_ERROR_STOP=1).

DO $$
DECLARE
    n BIGINT;
BEGIN
    IF to_regclass('public.scored_outcomes') IS NOT NULL THEN
        EXECUTE 'SELECT count(*) FROM scored_outcomes' INTO n;
        RAISE NOTICE 'v138: dropping scored_outcomes (% rows)', n;
    ELSE
        RAISE NOTICE 'v138: scored_outcomes absent — nothing to drop';
    END IF;

    IF to_regclass('public.signal_queue') IS NOT NULL THEN
        EXECUTE 'SELECT count(*) FROM signal_queue' INTO n;
        RAISE NOTICE 'v138: signal_queue KEPT (% rows) — staging for research '
                     'provenance, to be re-wired, not residue', n;
    END IF;
END $$;

DROP TABLE IF EXISTS scored_outcomes;
