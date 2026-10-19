-- v009: catalog + catalog_installs tables
--
-- Generalised catalog/registry surface for installable bundles. Today's
-- kinds: 'design_system' (open-design seed). Future kinds: 'skill_pack',
-- 'codebook', 'artifact_skill', etc. Same plumbing covers them all.
--
-- Per-entity table by design: each entity's PG holds its own view of the
-- catalog (seeded identically by migration) plus its own install state.
-- Future cross-entity publishing pushes new rows into every entity's
-- table via a fan-out, not a central PG query.

CREATE TABLE IF NOT EXISTS catalog_entries (
    -- Identity
    catalog_id          BIGSERIAL    PRIMARY KEY,
    kind                TEXT         NOT NULL,           -- 'design_system' | 'skill_pack' | ...
    slug                TEXT         NOT NULL,           -- stable id within (kind)
    title               TEXT         NOT NULL,
    summary             TEXT,                            -- one-line description for the catalog browser

    -- Manifest (kind-specific schema, JSONB for flexibility)
    manifest            JSONB        NOT NULL DEFAULT '{}'::jsonb,

    -- Asset transport hints (bytes live on the platform; bridge serves on demand)
    asset_bundle_uri    TEXT,                            -- 'bridge://catalog/design_system/<slug>' etc.
    asset_size_bytes    BIGINT,                          -- approximate, for UI display

    -- Attribution
    publisher_name      TEXT         NOT NULL,           -- 'OpenDesign' | '<entity display_name>' | 'Privatae'
    publisher_url       TEXT,                            -- 'https://github.com/nexu-io/open-design' or entity profile
    publisher_entity_id INTEGER,                         -- set when an entity publishes; else NULL
    origin              TEXT         NOT NULL,           -- 'builtin' | 'external:<source>' | 'entity:<id>'

    -- Catalog state
    public              BOOLEAN      NOT NULL DEFAULT TRUE,
    price_micro         BIGINT,                          -- NULL = free; in microcents (matches wallet)

    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    UNIQUE (kind, slug)
);

CREATE INDEX IF NOT EXISTS idx_catalog_entries_kind
    ON catalog_entries (kind) WHERE public;

CREATE INDEX IF NOT EXISTS idx_catalog_entries_origin
    ON catalog_entries (origin);

-- Tracks which catalog entries this entity has installed and where the
-- assets landed. Uninstall removes the row + cleans up the install_path.
CREATE TABLE IF NOT EXISTS catalog_installs (
    install_id          BIGSERIAL    PRIMARY KEY,
    entity_id           INTEGER      NOT NULL,
    catalog_id          BIGINT       NOT NULL REFERENCES catalog_entries(catalog_id) ON DELETE CASCADE,
    install_path        TEXT         NOT NULL,           -- ContextStore prefix, e.g. 'factory-assets/design-systems/airbnb/'
    status              TEXT         NOT NULL DEFAULT 'installed',  -- 'pending' | 'installed' | 'removed'
    installed_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_used_at        TIMESTAMPTZ,

    UNIQUE (entity_id, catalog_id)
);

CREATE INDEX IF NOT EXISTS idx_catalog_installs_entity
    ON catalog_installs (entity_id, status);
