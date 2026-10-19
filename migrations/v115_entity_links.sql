-- v115_entity_links.sql
--
-- entity_links — the host's saved links. New table; `entity_links` appears
-- nowhere else in the tree (verified 2026-08-26).
--
-- THE GAP. There was no saved-link surface anywhere on the platform. Measured
-- 2026-08-26: a semantic search of the cseries namespace for "save a link or url
-- so it persists" returned nothing of the kind, and the only lexical hit for
-- "bookmark" was inside a minified bundle in the community app. So a user who
-- found something worth keeping had nowhere to put it, and the host had no way
-- to answer "what have I saved about X".
--
-- It bites hardest in the Browser app, which is exactly where people find
-- things. That sidecar is scale-to-zero by design (heavy
-- sidecars run on demand, never 24/7) and holds its cookies in memory only, so
-- everything a session learns dies with the session.
--
-- WHY NOT JUST RUN linkding. It is already admitted in the service catalog
-- (webapp.catalog_templates, status=admitted since 2026-07-22) and any customer
-- can deploy it. But MEASURED on the exact admitted digest 2026-08-26, it idles
-- at 148 MiB and reaches 176 MiB under light use — not because it does much, but
-- because it is Python under uWSGI with two worker processes. The base plan
-- gives a whole pod 2Gi, so bundling it everywhere would spend ~7% of every
-- customer's machine, permanently, on bookmark storage most of them never open.
-- A plan's memory budget covers the whole pod and density is set by requests, so
-- that is a real per-customer cost.
--
-- This table is the other half: link memory every plan gets for free, in the
-- pod's own database, where the rest of the host's memory already lives.
-- linkding stays in the store as the full app — browser extension, real UI —
-- for people who want it, and syncs against this.
--
-- ENTITY-SCOPED like every other table here. The tool runs behind the same
-- capability mask as `context` and its SQL runs through SandboxedPool, so a
-- dispatched agent's grant bounds it exactly as it bounds the context store.

CREATE TABLE IF NOT EXISTS entity_links (
    id           BIGSERIAL   PRIMARY KEY,
    entity_id    INTEGER     NOT NULL,
    url          TEXT        NOT NULL,
    title        TEXT,
    -- What the user or the host said about it when saving. Free text: this is
    -- the "why did I keep this" line, and it is what search hits most often.
    note         TEXT,
    -- Free-text tags, lowercased by the writer. An array rather than a join
    -- table on purpose — this is a per-host list measured in hundreds, and a
    -- second table would buy nothing but a join.
    tags         TEXT[]      NOT NULL DEFAULT '{}',
    -- Where it came from: 'browser' (the sidecar), 'chat', 'research', or an
    -- app id. Free text rather than an enum: a CHECK here would reject rows for
    -- a source nobody has thought of yet, and this is provenance, not control.
    source       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per (host, url). Saving the same page twice updates the existing row
-- rather than growing a duplicate — the writer relies on this for its upsert.
CREATE UNIQUE INDEX IF NOT EXISTS entity_links_entity_url_key
    ON entity_links (entity_id, url);

-- The list view: newest first, per host.
CREATE INDEX IF NOT EXISTS entity_links_entity_created
    ON entity_links (entity_id, created_at DESC);

-- Tag filtering.
CREATE INDEX IF NOT EXISTS entity_links_tags
    ON entity_links USING GIN (tags);

-- Search across title + note + url in one shot. Expression index rather than a
-- stored tsvector column: no trigger to keep in step, and this corpus is small
-- enough that the index rebuild on write is not worth a second moving part.
-- coalesce on every part — a NULL anywhere would otherwise NULL the whole
-- concatenation and silently drop the row out of every search.
CREATE INDEX IF NOT EXISTS entity_links_search
    ON entity_links USING GIN (to_tsvector('english',
        coalesce(title, '') || ' ' || coalesce(note, '') || ' ' || coalesce(url, '')));

COMMENT ON TABLE entity_links IS
    'The host''s saved links — its link memory. Entity-scoped. Every plan gets '
    'this; the linkding catalog app is the optional full bookmark product that '
    'syncs against it, not a replacement for it.';

COMMENT ON COLUMN entity_links.note IS
    'Why this was kept. The line search hits most often.';
