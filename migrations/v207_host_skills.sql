-- v207: the skills a Host writes for itself, one row per version.
--
-- A Host writes a skill with `context(action='write', path='skills/<name>')`
-- (runtime/context_ops.py) and a person edits one in the Skills app
-- (server/routes/skills.py). Every write is checked against the skill
-- standard (runtime/skill_standard.py) and lands here as a NEW version; the
-- version it replaces is kept. runtime/host_skills.py is the one writer.
--
--   name           the path's name, `skills/<name>`. An ACTIVE version's name
--                  meets the standard (the CHECK below); a retired version
--                  moved in from entity_context keeps the name its path had.
--   version        1, 2, 3 … per (entity, name), never reused.
--   body           the full text as written, frontmatter included.
--   description, allowed_tools, mcp_server, sources
--                  the frontmatter, as the validator parsed it. `sources` is a
--                  JSON array of {kind, ref, …}: what the skill was rewritten
--                  from (repo at a commit, a URL, mcp:<server>, conversation).
--   author         'host' (a context write) or 'person' (the Skills app).
--   thread_id, session_id
--                  the conversation and the turn's session the write was made
--                  in; session_id is the value tool_call_log records for the
--                  same call. A provenance pointer only, so no foreign key: a
--                  skill write never fails on where it was written.
--   edited_from    the version this text came from (the previous version, or
--                  the one a restore brought back).
--   status         'active' or 'retired'. One active version per name (the
--                  partial unique index). superseded_at is when a version
--                  stopped being active; NULL on a version that never was.
--
-- THE DATA MOVE. Before this, `context(action='write', path='skills/x.md')`
-- stored an entity_context row with no check of any kind, and
-- nothing loaded it. Every such row moves here as a RETIRED version: nothing
-- unvalidated becomes active, nothing is lost, and the Host can rewrite any of
-- them to the standard. A moved version's author is 'host' (the `context`
-- tool was the writer of these rows) and its sources
-- record the row it came from: [{"kind": "entity_context", "ref": <path>}].
-- Two kinds of row stay where they are:
--   - a row whose text carries an `Origin: external:` marker. Those are the
--     coding-agent packs seeded by v007/v008, and migrations/v206 deletes
--     them.
--   - a row whose bytes live on the Host's S3 bucket or home server
--     (content is ContextStore's pointer marker, brain/mastercode/
--     context_store.py _EXTERNAL_MARKER). SQL cannot fetch those bytes, so the
--     row stays in entity_context, where `context` still reads it
--     (context_ops falls back to the row when no Host skill has that name).
-- The move and the delete are ONE statement in one transaction, so a failure
-- part way moves nothing, and a re-run finds nothing left to move.

BEGIN;

CREATE TABLE IF NOT EXISTS host_skills (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL
                    REFERENCES entity_registry(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL CHECK (version >= 1),
    body            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    allowed_tools   TEXT[] NOT NULL DEFAULT '{}',
    mcp_server      TEXT,
    sources         JSONB NOT NULL DEFAULT '[]'::jsonb,
    author          TEXT NOT NULL CHECK (author IN ('host', 'person')),
    thread_id       UUID,
    session_id      TEXT,
    edited_from     INTEGER,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'retired')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    superseded_at   TIMESTAMPTZ,
    CONSTRAINT host_skills_version_once UNIQUE (entity_id, name, version),
    -- The standard's name rule (skill_standard.NAME_RE, NAME_MAX), held by the
    -- table too for every version that can ride a turn.
    CONSTRAINT host_skills_active_name_is_standard
        CHECK (status <> 'active'
               OR (name ~ '^[a-z0-9]+(-[a-z0-9]+)*$' AND length(name) <= 64))
);

-- One active version per name.
CREATE UNIQUE INDEX IF NOT EXISTS uq_host_skills_one_active
    ON host_skills (entity_id, name)
    WHERE status = 'active';

-- The Skills app's list and a skill's history.
CREATE INDEX IF NOT EXISTS ix_host_skills_entity_name_version
    ON host_skills (entity_id, name, version DESC);

WITH moving AS (
    SELECT ec.id, ec.entity_id, ec.file_path,
           COALESCE(ec.content, '') AS content,
           COALESCE(ec.updated_at, ec.created_at, now()) AS at,
           regexp_replace(substr(ec.file_path, length('skills/') + 1), '\.md$', '') AS name
    FROM entity_context ec
    WHERE ec.file_path LIKE 'skills/%'
      AND position('Origin: external:' IN COALESCE(ec.content, '')) = 0
      AND ec.content IS DISTINCT FROM U&'\241E__context_external__\241E'
      AND regexp_replace(substr(ec.file_path, length('skills/') + 1), '\.md$', '') <> ''
),
numbered AS (
    SELECT m.*,
           row_number() OVER (PARTITION BY m.entity_id, m.name ORDER BY m.at, m.id)
           + COALESCE((SELECT max(h.version) FROM host_skills h
                       WHERE h.entity_id = m.entity_id AND h.name = m.name), 0) AS version
    FROM moving m
),
moved AS (
    INSERT INTO host_skills (entity_id, name, version, body, sources, author, status, created_at)
    SELECT n.entity_id, n.name, n.version, n.content,
           jsonb_build_array(jsonb_build_object('kind', 'entity_context', 'ref', n.file_path)),
           'host', 'retired', n.at
    FROM numbered n
    RETURNING 1
)
DELETE FROM entity_context ec
USING moving m
WHERE ec.id = m.id;

COMMIT;
