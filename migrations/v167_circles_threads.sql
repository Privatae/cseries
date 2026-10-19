-- v167 — circles_threads + circles_thread_links.
--
-- Foundation table for Circles threads. A Circles thread is a scope, the way a Workbench project already
-- is one: every turn, summary and artifact written inside it is stamped
-- with {kind: "thread", ref: <id>}. This migration lands only the two tables
-- and their indexes — the descriptor plumbing lives in memory_engine.py,
-- circles.py and session.py.
--
-- uuid default follows existing practice: pgcrypto's gen_random_uuid(), the
-- same function init-db.sql already uses throughout (e.g. init-db.sql:48
-- `entity_uuid UUID DEFAULT gen_random_uuid()`, :1908, :1921). pgcrypto is
-- built into the entity's Postgres 17 image with no extra extension needed
-- (Dockerfile.postgres: "PostgreSQL 17 + pgvector + Apache AGE"; pgcrypto is
-- already `CREATE EXTENSION IF NOT EXISTS pgcrypto` at init-db.sql:12).
--
-- entity_id carries the FK every entity-owned table in init-db.sql carries
-- (entity_episodes:109, entity_memory:136, entity_sessions:314 — all
-- `INTEGER NOT NULL REFERENCES entity_registry(id) ON DELETE CASCADE`).
-- Without it a thread row can name an entity that does not exist and survives
-- the entity's deletion as an orphan nothing lists.
CREATE TABLE IF NOT EXISTS circles_threads (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id           INTEGER NOT NULL
                        REFERENCES entity_registry(id) ON DELETE CASCADE,
    title               TEXT,
    topic               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at           TIMESTAMPTZ,
    origin              TEXT NOT NULL DEFAULT 'user'
                        CHECK (origin IN ('user', 'split')),
    parent_thread_id    UUID NULL REFERENCES circles_threads(id)
);

CREATE INDEX IF NOT EXISTS idx_circles_threads_entity_created
    ON circles_threads (entity_id, created_at DESC);

-- Links are events, not a graph: a read of another thread on the
-- user's behalf, or a Split, each write one row. Never touched by the memory
-- graph (memory_graph.py is untouched).
-- A thread never links to ITSELF: a link records one thread reaching into
-- another, so a self-link is a bug's signature (a non-canonical ref compared
-- as a string against the row's canonical id) and it renders the thread as
-- its own RELATED item. Enforced here as well as in write_link, because the
-- clause that produces it is a string compare in a different module.
CREATE TABLE IF NOT EXISTS circles_thread_links (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL
                    REFERENCES entity_registry(id) ON DELETE CASCADE,
    from_thread     UUID NOT NULL REFERENCES circles_threads(id),
    to_thread       UUID NOT NULL REFERENCES circles_threads(id),
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason          TEXT NOT NULL CHECK (reason IN ('recall', 'split')),
    CONSTRAINT circles_thread_links_not_self CHECK (from_thread <> to_thread)
);

CREATE INDEX IF NOT EXISTS idx_circles_thread_links_from
    ON circles_thread_links (entity_id, from_thread);

CREATE INDEX IF NOT EXISTS idx_circles_thread_links_to
    ON circles_thread_links (entity_id, to_thread);
