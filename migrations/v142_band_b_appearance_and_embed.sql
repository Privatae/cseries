-- v142: entity_appearance + embed_jobs move out of the request path
--
-- Two tables the kernel has been creating for itself on every request. The
-- appearance routes ran their whole CREATE-plus-ten-ALTERs block before serving
-- a wallpaper; the embed module carried a create-if-missing helper for its job
-- queue. Schema belongs in the declared files, applied once at boot by
-- entrypoint.sh, so the running kernel needs no DDL rights at all. Nothing
-- about either feature changes — the reads and writes are untouched, only the
-- self-bootstrapping is gone.
--
-- THE TWO TABLES NEEDED VERY DIFFERENT AMOUNTS OF WORK, and the difference is
-- the point. entity_appearance is declared NOWHERE — not in init-db.sql, not in
-- any earlier migration — so its full CREATE moves here. embed_jobs is already
-- declared at init-db.sql:1136, so the table does NOT move here and only the
-- two indexes, which no declared schema carries, do. Check before you copy a
-- runtime CREATE into a migration: the runtime DDL is a defensive mirror far
-- more often than it is the only declaration, and a second CREATE of an
-- already-declared table is new drift wearing the clothes of a fix.
--
-- These statements are written against the measured LIVE shape of all 8
-- hosts, not against what the feature files would produce today. Where the
-- two disagree, LIVE WINS and this file only ever ADDS what is missing. That is
-- not a style preference: entrypoint.sh runs every migration with
-- ON_ERROR_STOP=1, so one statement that assumes a column's current type or
-- default is a host that does not boot.
--
--
-- entity_appearance — no disagreement, and one non-difference worth naming.
--
-- All 8 hosts carry the same 13 columns with the same types, defaults and
-- nullability, and the same single primary-key index on entity_id. The
-- CREATE TABLE below is therefore a pure no-op on all of them and only matters
-- for a brand-new entity.
--
-- Two hosts (102 and 3) store those columns in a different PHYSICAL ORDER,
-- because they took the ALTERs in a different sequence than the others years
-- apart. THAT IS NOT A SHAPE MISMATCH AND IT GETS NO STATEMENT HERE. Column
-- order is not addressable in PostgreSQL without rewriting the table, every
-- query in server/routes/appearance.py names its columns explicitly, and
-- "fixing" it would mean a DROP and a rebuild of a table holding live rows on
-- a host that must come up. If you are reading this because the survey showed
-- two variants: that is the variant, it is cosmetic, leave it.
--
--
-- embed_jobs — THE TABLE IS ALREADY DECLARED. This adds two indexes, nothing
-- else.
--
-- `init-db.sql:1136` declares embed_jobs, carrying every column the runtime
-- block had. So there is no table to move here, and v142 deliberately does NOT
-- restate it: two declarations of one table is how a schema drifts, and a
-- second CREATE sitting in migrations/ would read as authoritative while
-- disagreeing with the real owner. If embed_jobs ever needs a column it goes
-- into init-db.sql and a migration carries the ALTER — never a fresh CREATE
-- here.
--
-- What IS a real delta, and why one long-lived Host looks different:
--
-- 1. DEFAULTS ON job_type AND target — LEFT ALONE, IN BOTH DIRECTIONS.
--    init-db.sql:1139-1140 declares DEFAULT 'full' and DEFAULT 'entity_memory'.
--    Seven hosts have them because that is what the declared schema produces.
--    The eighth has neither, because its table
--    was created by the runtime block in server/routes/embed.py instead, and
--    that block never carried the defaults. That Host is the outlier, and this
--    migration does not correct it: no SET DEFAULT to bring it into line, no
--    DROP DEFAULT to drag the other seven out of line. Live wins. Every writer
--    in embed.py supplies both values explicitly, so the default is never
--    reached and the divergence is invisible to the feature — which is exactly
--    why it is not worth a statement that could fail on a host that must boot.
--
-- 2. THE TWO INDEXES — the genuine delta, and the only thing added below.
--    ix_embed_jobs_entity and ix_embed_jobs_active appear in NO declared
--    schema: not in init-db.sql, not in any earlier migration. Only the runtime
--    block declared them, and only that one Host has them — because that block's
--    ensure_table() helper is what created its table, and it has had no
--    caller anywhere in the tree for a long time. Deleting that helper without
--    these two statements would lose the indexes permanently. They are a no-op
--    on that Host and finally land on the other seven what the code always
--    intended. Adding an index is additive, cannot fail on shape, and changes
--    no result — only the plan for the two queries embed.py runs most.
--
-- No vector or halfvec column is involved on either table, so nothing here
-- depends on the pgvector divergence that governs user_context.
--
-- Idempotent and forward-only, per migrations/README.md.

BEGIN;

-- ── entity_appearance ────────────────────────────────────────────────
-- Was server/routes/appearance.py SCHEMA_SQL (1 CREATE + 10 ALTERs, run by
-- _ensure_schema on nearly every request). Written out flat: the ALTERs only
-- ever existed to catch up deployments older than each column, and a fresh
-- CREATE already carries all of them.

CREATE TABLE IF NOT EXISTS entity_appearance (
    entity_id                 INTEGER     PRIMARY KEY,
    wallpaper                 TEXT        NOT NULL DEFAULT 'default',
    image_bytes               BYTEA,
    image_content_type        TEXT,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    desktop_layout            JSONB,
    wallpaper_mobile          TEXT,
    image_bytes_mobile        BYTEA,
    image_content_type_mobile TEXT,
    desktop_artifact_id       INTEGER,
    desktop_entry             TEXT,
    network_artifact_id       INTEGER,
    network_entry             TEXT
);

-- And the catch-up path, kept verbatim for any entity provisioned between the
-- original four columns and today. All ten are no-ops on all 8 hosts as
-- measured; they exist for a database this migration meets mid-history.
ALTER TABLE entity_appearance ADD COLUMN IF NOT EXISTS image_bytes               BYTEA;
ALTER TABLE entity_appearance ADD COLUMN IF NOT EXISTS image_content_type        TEXT;
ALTER TABLE entity_appearance ADD COLUMN IF NOT EXISTS desktop_layout            JSONB;
ALTER TABLE entity_appearance ADD COLUMN IF NOT EXISTS wallpaper_mobile          TEXT;
ALTER TABLE entity_appearance ADD COLUMN IF NOT EXISTS image_bytes_mobile        BYTEA;
ALTER TABLE entity_appearance ADD COLUMN IF NOT EXISTS image_content_type_mobile TEXT;
ALTER TABLE entity_appearance ADD COLUMN IF NOT EXISTS desktop_artifact_id       INTEGER;
ALTER TABLE entity_appearance ADD COLUMN IF NOT EXISTS desktop_entry             TEXT;
ALTER TABLE entity_appearance ADD COLUMN IF NOT EXISTS network_artifact_id       INTEGER;
ALTER TABLE entity_appearance ADD COLUMN IF NOT EXISTS network_entry             TEXT;

-- ── embed_jobs ───────────────────────────────────────────────────────
-- NO CREATE TABLE. The table is init-db.sql:1136's — see the header. These two
-- indexes are the entire delta: server/routes/embed.py's EMBED_JOBS_SCHEMA was
-- the only place they were ever declared, and it is being deleted.

-- Guarded, because these are the only statements in this file that depend on a
-- table it does not declare, and the outlier Host above proves the state they
-- would abort in was real: its embed_jobs carries no defaults, which means the
-- table came from the runtime block and not from init-db.sql — so it was once
-- a host with entity_registry and no embed_jobs. Deleting server/routes/embed.py's
-- ensure_table() removes the backstop that closed that hole. All 8 hosts have
-- the table today, so this cannot bite now; the guard is here so it cannot bite
-- later either. An unguarded CREATE INDEX on a missing table aborts the whole
-- migration under ON_ERROR_STOP=1, and that is a host that does not boot.
DO $$
BEGIN
    IF to_regclass('public.embed_jobs') IS NULL THEN
        RAISE NOTICE 'v142: embed_jobs absent — init-db.sql declares it, so this '
                     'database predates that declaration; nothing to index';
    ELSE
        CREATE INDEX IF NOT EXISTS ix_embed_jobs_entity ON embed_jobs(entity_id, status);
        CREATE INDEX IF NOT EXISTS ix_embed_jobs_active ON embed_jobs(status)
            WHERE status IN ('pending','running','paused');
    END IF;
END $$;

COMMIT;
