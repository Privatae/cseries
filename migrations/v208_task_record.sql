-- v208: the task record — what every thread of one task shares.
--
-- Scoped to a project (the task's thread), in the Host's own database, written
-- through one Host tool (`task_record`, runtime/task_record.py) and by the
-- readers that keep what they fetched (read_url, web_search, research).
--
--   goal_checks     the goal, written as checks before any reading. A check is
--                   a question the finished work must answer: open, answered
--                   (a live finding is linked to it) or unanswerable (every
--                   found source is covered).
--   task_coverage   one row per unit of source — a URL, a file at a commit, a
--                   chunk of an indexed dataset — found -> claimed -> covered.
--                   A covered row has an outcome: findings, nothing_relevant or
--                   unreadable. A unit is stored under one spelling
--                   (task_record.source_key).
--   task_sources    the text the platform itself fetched for a source, kept
--                   for the task with the range read; one row per distinct
--                   text (version = sha256 of it). A finding's quote is checked
--                   against these, so it cannot come from anywhere else.
--   task_findings   one row per finding: the claim, the source's exact words,
--                   the source and the version the words were found in, the
--                   checks it bears on. live, superseded or stale; superseded,
--                   never edited.
--
-- Nothing is written until the project has a goal check: the readers ask
-- first (task_record.record_open), so an ordinary project conversation that
-- searches the web fills none of these tables.

BEGIN;

CREATE TABLE IF NOT EXISTS goal_checks (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL
                    REFERENCES entity_registry(id) ON DELETE CASCADE,
    project_id      INTEGER NOT NULL
                    REFERENCES projects(id) ON DELETE CASCADE,
    question        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'answered', 'unanswerable')),
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_goal_checks_project ON goal_checks (entity_id, project_id, id);

CREATE TABLE IF NOT EXISTS task_coverage (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL
                    REFERENCES entity_registry(id) ON DELETE CASCADE,
    project_id      INTEGER NOT NULL
                    REFERENCES projects(id) ON DELETE CASCADE,
    source_ref      TEXT NOT NULL,
    title           TEXT,
    status          TEXT NOT NULL DEFAULT 'found'
                    CHECK (status IN ('found', 'claimed', 'covered')),
    outcome         TEXT
                    CHECK (outcome IS NULL OR outcome IN ('findings', 'nothing_relevant', 'unreadable')),
    found_via       TEXT,
    claimed_by      TEXT,
    claimed_at      TIMESTAMPTZ,
    covered_by      TEXT,
    covered_at      TIMESTAMPTZ,
    version         TEXT,
    read_range      JSONB,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT task_coverage_once UNIQUE (entity_id, project_id, source_ref),
    CONSTRAINT task_coverage_covered_has_outcome
        CHECK (status <> 'covered' OR outcome IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_task_coverage_project ON task_coverage (entity_id, project_id, status, id);

CREATE TABLE IF NOT EXISTS task_sources (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL
                    REFERENCES entity_registry(id) ON DELETE CASCADE,
    project_id      INTEGER NOT NULL
                    REFERENCES projects(id) ON DELETE CASCADE,
    source_ref      TEXT NOT NULL,
    version         TEXT NOT NULL,
    text            TEXT NOT NULL,
    read_range      JSONB NOT NULL DEFAULT '{}'::jsonb,
    via             TEXT,
    read_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT task_sources_once UNIQUE (entity_id, project_id, source_ref, version)
);
CREATE INDEX IF NOT EXISTS ix_task_sources_ref ON task_sources (entity_id, project_id, source_ref, read_at DESC);

CREATE TABLE IF NOT EXISTS task_findings (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL
                    REFERENCES entity_registry(id) ON DELETE CASCADE,
    project_id      INTEGER NOT NULL
                    REFERENCES projects(id) ON DELETE CASCADE,
    claim           TEXT NOT NULL,
    quote           TEXT NOT NULL,
    source_ref      TEXT NOT NULL,
    source_version  TEXT NOT NULL,
    source_read_at  TIMESTAMPTZ NOT NULL,
    check_ids       BIGINT[] NOT NULL DEFAULT '{}',
    thread_id       TEXT,
    session_id      TEXT,
    status          TEXT NOT NULL DEFAULT 'live'
                    CHECK (status IN ('live', 'superseded', 'stale')),
    superseded_by   BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_task_findings_project ON task_findings (entity_id, project_id, status, id);

COMMIT;
