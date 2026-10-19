-- C-Series OS — Entity Brain Database Schema
--
-- Single-entity personal computer. Entity ID 1 is always "this entity".
-- PostgreSQL 16 + pgvector. Runs embedded inside the container.
--
-- Tables: 21 (brain core only — no platform, no multi-entity mesh)
--
-- Schema derived from Privatae host DB, stripped to brain essentials.

-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- ============================================================
-- IDENTITY: Entity Registry
-- ============================================================
-- Single row (id=1) for the entity running on this computer.
-- Schema matches host so brain code works without modification.

CREATE TABLE entity_registry (
    id                  SERIAL PRIMARY KEY,
    entity_uuid         UUID DEFAULT gen_random_uuid() UNIQUE,
    entity_type         VARCHAR(20) NOT NULL DEFAULT 'personal'
                        CHECK (entity_type IN ('personal','native','operator')),
    display_name        TEXT NOT NULL,
    email               VARCHAR(255) UNIQUE,
    phone               VARCHAR(20) UNIQUE,
    identity_verified   BOOLEAN DEFAULT false,
    operator_id         VARCHAR(100),
    codebook_id         INTEGER,
    privatae_entity_id  BIGINT,            -- ID on the network (if connected)
    master_directive    TEXT NOT NULL DEFAULT '',
    directive_hash      VARCHAR(64),
    directive_visibility VARCHAR(20) DEFAULT 'public'
                        CHECK (directive_visibility IN ('public','restricted','classified')),
    license_tier        VARCHAR(30) NOT NULL DEFAULT 'standard'
                        CHECK (license_tier IN ('standard','pro','enterprise','admin')),
    permission_overrides JSONB,
    seed_weights        JSONB NOT NULL DEFAULT '{}',
    current_weights     JSONB NOT NULL DEFAULT '{}',
    behavioral_params   JSONB,
    envelope_bounds     JSONB NOT NULL DEFAULT '{}',
    context_namespace   VARCHAR(100) UNIQUE,
    active_goals        JSONB,
    sandbox_type        VARCHAR(30),
    sandbox_config      JSONB,
    status              VARCHAR(20) DEFAULT 'active'
                        CHECK (status IN ('enrolled','active','suspended','debugging','archived','revoked')),
    created_at          TIMESTAMPTZ DEFAULT now(),
    enrolled_at         TIMESTAMPTZ,
    activated_at        TIMESTAMPTZ,
    last_active_at      TIMESTAMPTZ,
    suspended_at        TIMESTAMPTZ,
    archived_at         TIMESTAMPTZ,
    inversion_mode      VARCHAR(20) DEFAULT 'classic',
    personality_mode    VARCHAR(20) DEFAULT 'sovereign',
    token_balance       BIGINT NOT NULL DEFAULT 100000,
    token_limit         BIGINT NOT NULL DEFAULT 100000,
    handler_entity_id   INTEGER,           -- NULL for personal computer
    dashboard_tier      VARCHAR DEFAULT 'personal',
    -- brain_loop_mode removed: retired-UnifiedBrain vestige, zero writers;
    -- readers were cleaned out of routes/brain.py too.
    daily_token_reset_at DATE,
    human_name          VARCHAR(255),
    identity_lock       VARCHAR(128),
    skill_weights       JSONB DEFAULT '{}'
);

CREATE INDEX idx_entity_registry_status ON entity_registry (status);

-- ============================================================
-- MEMORY
-- ============================================================

CREATE TABLE entity_memory (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entity_registry(id) ON DELETE CASCADE,
    content         TEXT NOT NULL,
    -- The platform's part of memory:
    -- the transcript (turn, summary), the person's standing instructions
    -- (preference incl. rules, relationship, constitutional), thread state
    -- (goal), task products (cited_fact, research) and the governance row.
    -- 'retired' is what a row of a type that no longer exists became
    -- (migration v235: metadata.prior_memory_type keeps the old name);
    -- nothing writes it. Shaped knowledge about the person lives in the
    -- Host's own schema `world`, not here.
    memory_type     VARCHAR(30) NOT NULL
                    CHECK (memory_type IN ('turn','summary','preference',
                        'relationship','constitutional','goal','cited_fact',
                        'research','governance_observation','retired')),
    source_session  VARCHAR(80),
    source_message_idx INTEGER,
    importance      DOUBLE PRECISION DEFAULT 1.0,
    confidence      DOUBLE PRECISION DEFAULT 1.0,
    access_count    INTEGER DEFAULT 0,
    last_accessed_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    visibility      TEXT NOT NULL DEFAULT 'default',
    metadata        JSONB DEFAULT '{}',
    status          TEXT DEFAULT 'active'
                    CHECK (status IN ('active','archived','suppressed','corrected')),
    correction_of   INTEGER REFERENCES entity_memory(id),
    corrected_by    INTEGER REFERENCES entity_memory(id),
    status_changed_at TIMESTAMPTZ,
    status_reason   TEXT,
    project_id      INTEGER,
    embedding       vector(1024),
    -- Multi-dim content embeddings (v044). The entity's active
    -- embedding model decides which column its content vectors land in:
    --   768  → embedding_768   (e.g. nomic-embed-text)
    --   1024 → embedding       (most of the catalog; the historical default)
    --   2048 → embedding_2048  (no catalog model today; column kept ready)
    --   4096 → embedding_4096  (e.g. qwen3-embedding 7.6B)
    -- embedding_model tags which model wrote the row.
    embedding_768   vector(768),
    embedding_2048  vector(2048),
    embedding_4096  vector(4096),
    embedding_model TEXT,
    -- Bi-temporal validity model. Every memory row
    -- carries a window during which it was the live "truth" for the
    -- entity. New writes: t_valid_from = NOW(), t_valid_until = NULL.
    -- On consolidation, when a memory is superseded by a newer one, set
    -- t_valid_until = NOW() and resolution_pointer = <new row's id>. All
    -- recall queries filter `WHERE t_valid_until IS NULL` by default;
    -- the audit path can time-travel. Encoded here so it's first-class
    -- on every entity from second-zero. Bridge of glass to the AGE-side
    -- (:Memory)-[:SUPERSEDED_BY]->(:Memory) graph.
    -- Resolution semantics for goal/unresolved memories.
    -- Set when the user (or the entity) marks a memory as completed/answered.
    -- The recent-active loader allows goal/unresolved into prompt context
    -- only when resolved_at IS NULL AND the row is still fresh. The
    -- consolidation decay path drops importance faster on resolved rows.
    -- Without this column those types had no exit condition and they
    -- contaminated every chat.
    resolved_at         TIMESTAMPTZ,
    t_valid_from        TIMESTAMPTZ NOT NULL DEFAULT now(),
    t_valid_until       TIMESTAMPTZ,
    resolution_pointer  INTEGER REFERENCES entity_memory(id),
    -- v096 lexical arm: GENERATED so every writer (15+ INSERT sites) is
    -- covered by ONE schema-level definition — per-writer stamping is the
    -- forgotten-site bug class user_context suffered (v084→v095). Second
    -- pass separator-normalizes so 'budget.csv'-style tokens are lexemes.
    search_tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english'::regconfig,
                            left(coalesce(content, ''), 100000) || ' ' ||
                            left(regexp_replace(coalesce(content, ''), '[._\-/]+', ' ', 'g'), 100000)
                        )) STORED,
    -- v216: when a turn row's words were last written, set by the turn
    -- writers at every write and rewrite (memory_engine._write_turn_row_landed,
    -- _write_turn_row_by_token_landed), never earlier than created_at. NULL on
    -- rows from before it and on rows other writers lay down: those are read
    -- by created_at, COALESCE(landed_at, created_at).
    -- migrations/v216_project_list_reads.sql adds it to existing databases.
    landed_at           TIMESTAMPTZ
);

CREATE INDEX idx_entity_memory_entity ON entity_memory (entity_id);
CREATE INDEX idx_entity_memory_search_tsv ON entity_memory USING gin (search_tsv);
CREATE INDEX idx_entity_memory_type ON entity_memory (entity_id, memory_type);
CREATE INDEX idx_entity_memory_importance ON entity_memory (entity_id, importance DESC);
CREATE INDEX idx_entity_memory_status ON entity_memory (status) WHERE status = 'active';
CREATE INDEX idx_memory_entity_status ON entity_memory (entity_id, status);
CREATE INDEX idx_entity_memory_embedding_hnsw ON entity_memory USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);
-- Multi-dim content columns. HNSW per dim column for the dims pgvector can
-- index. NOTE: pgvector's HNSW caps at 2000 dimensions, so embedding_2048 and
-- embedding_4096 get NO HNSW here — they fall back to sequential scan, which
-- is the accepted trade-off for the high-dim models (see v044).
--
-- SUPERSEDED, and the text that was here is why this note exists: it said the
-- 2048 index "is created lazily by ensure_embedding_index()". That function was
-- deleted — it had no callers, and all four indexes it
-- could have built are declared in .sql already (v044, v070, v071, and v151 for
-- the 1024d `embedding` index at line 233 — that one was declared ONLY here
-- until v151, and this file runs only on an empty database, so its population
-- was never guaranteed) and present on all 8 live hosts. The v071 halfvec
-- conversion is also what
-- made the >2000d caveat obsolete: 2048 and 2560 ARE indexed today, as halfvec
-- under the 4000d halfvec cap. 4096 stays unindexed, which is still the trade.
-- Nothing creates an entity_memory index at runtime any more.
-- 768d. DECLARED TWICE, AND THE TWO DECLARATIONS DIFFER — read this before
-- "fixing" either. This one is a FULL index; v044_multi_dim_embeddings.sql:40
-- builds the same NAME as a PARTIAL index (WHERE embedding_768 IS NOT NULL, no
-- WITH clause). A host ends up with one or the other by PROVENANCE, and the
-- mechanism is this file, not IF NOT EXISTS: v044's CREATE carries IF NOT
-- EXISTS, the one below does not (it would ERROR on an existing index), and
-- init-db.sql runs only on a database that is empty at boot. So a fresh host
-- builds this full one and v044 then skips; a host that predates this line got
-- v044's partial one. Both shapes exist on live hosts. (A survey that reads
-- index PRESENCE by name does not distinguish the two shapes.)
--
-- It does NOT cost a scan, which is why converging them is not worth a DROP +
-- rebuild on a live host. Every vector read path on this column carries the
-- partial index's own predicate — `AND embedding_768 IS NOT NULL` — so the
-- planner uses EITHER shape. Measured on 5,000 rows in PG 17.10: both shapes
-- plan as `Index Scan using idx_entity_memory_embedding_768_hnsw`, neither a
-- Seq Scan.
--
-- HOW TO RE-DERIVE THAT, because the sweep is not a grep. The column name is
-- interpolated from EMBEDDING_COLUMN_BY_DIM, so searching for the literal
-- `embedding_768` finds only the map entry, an IS NOT NULL presence probe in
-- memory_retrieval.py and ANY_EMBEDDED_SQL in server/routes/context.py — no
-- distance read among them. Go through active_embedding_column's call sites
-- instead: every one that does a bare `ORDER BY <col> <=> <const>` on
-- entity_memory carries the predicate. They live in memory_retrieval.py,
-- memory_engine.py (x2), memory_consolidation.py, signal_retrieval.py,
-- teaching_deduplicator.py, timeline_memory.py, constitution/context.py (x2),
-- brain/mastercode/brain/brain.py and server/synaptive_handler.py. Deliberately
-- named by FILE and not by line: three line numbers in this block have already
-- gone stale from edits to the block itself.
--
-- The exposure is a future query that omits the predicate AND is otherwise
-- HNSW-usable. Omitting it is not sufficient on its own — the two paths that
-- already omit it diverge not at all, measured on both shapes: the graph arm in
-- memory_retrieval.py touches the column only in the target list under
-- `id = ANY(...)` and uses the pkey either way, and memory_engine.retrieve's
-- blended score orders by a composite arithmetic expression (importance x tier
-- x recency x similarity) and sequential-scans either way. A bare
-- `ORDER BY embedding_768 <=> $1` with no predicate is the shape that would use
-- the index on the five hosts measured to hold this one and scan on the two
-- holding v044's (one host unmeasured). If that query is ever
-- written, converge the index first.
CREATE INDEX idx_entity_memory_embedding_768_hnsw
    ON entity_memory USING hnsw (embedding_768 vector_cosine_ops) WITH (m=16, ef_construction=64);
CREATE INDEX idx_entity_memory_embedding_model
    ON entity_memory (entity_id, embedding_model) WHERE embedding_model IS NOT NULL;
-- Active-only fast path. ~all recall queries gain t_valid_until IS NULL;
-- partial index pre-filters the live set so the vector op runs over a
-- smaller candidate pool.
CREATE INDEX idx_entity_memory_live ON entity_memory (entity_id, t_valid_from DESC)
    WHERE t_valid_until IS NULL;
-- v216: what the projects list and the project search read per project (a
-- project's newest turn row; its newest Host reply by when it landed, for
-- last_landed_at). The expressions are the ones server/routes/projects.py
-- writes. migrations/v216_project_list_reads.sql adds them to existing
-- databases; the definitions must stay identical to these.
CREATE INDEX idx_entity_memory_project_turns
    ON entity_memory (entity_id, project_id, created_at)
    WHERE memory_type = 'turn' AND project_id IS NOT NULL;
CREATE INDEX idx_entity_memory_project_replies
    ON entity_memory (entity_id, project_id, (COALESCE(landed_at, created_at)))
    WHERE memory_type = 'turn' AND project_id IS NOT NULL
      AND (metadata->>'role') = 'assistant';
-- v218: the rows that carry the id their sender gave a message
-- (metadata.client_id: a nudge's user row, a message's user row, the Index's
-- marker; an array of ids for one turn joined from several messages; in PII
-- mode their digests under metadata.client_id_digest), found by containment of
-- one id. migrations/v218_entity_memory_client_id.sql adds it to existing
-- databases; the definitions must stay identical.
CREATE INDEX idx_entity_memory_client_id
    ON entity_memory USING gin ((COALESCE(metadata->'client_id', metadata->'client_id_digest')) jsonb_path_ops)
    WHERE COALESCE(metadata->'client_id', metadata->'client_id_digest') IS NOT NULL;
-- v219: what the Circles thread list reads per thread (a thread's newest
-- turn row; its newest Host reply by when it landed). The WHERE is the
-- transcript's thread arm (circles_threads._THREAD_ROWS, _THREAD_REPLIES).
-- migrations/v219_circles_page_marks.sql adds them to existing databases;
-- the definitions must stay identical.
CREATE INDEX idx_entity_memory_thread_rows
    ON entity_memory (entity_id, (metadata->'source_context'->>'ref'),
                      (COALESCE(landed_at, created_at)))
    WHERE memory_type = 'turn' AND status = 'active' AND project_id IS NULL
      AND (metadata->'source_context'->>'kind') = 'thread'
      AND COALESCE(metadata->>'app_id', '') NOT IN ('circles-replay', 'circles-ab', 'scheduled_task');
CREATE INDEX idx_entity_memory_thread_replies
    ON entity_memory (entity_id, (metadata->'source_context'->>'ref'),
                      (COALESCE(landed_at, created_at)))
    WHERE memory_type = 'turn' AND status = 'active' AND project_id IS NULL
      AND (metadata->'source_context'->>'kind') = 'thread'
      AND COALESCE(metadata->>'app_id', '') NOT IN ('circles-replay', 'circles-ab', 'scheduled_task')
      AND (metadata->>'role') = 'assistant'
      AND COALESCE(metadata->>'source_kind', '') NOT IN ('thread_marker', 'connector_pick', 'index_pick');
-- v219 too: the original conversation's rows (circles_threads._ORIGINAL_ROWS)
-- and its Host replies (_ORIGINAL_REPLIES).
CREATE INDEX idx_entity_memory_original_rows
    ON entity_memory (entity_id, created_at, landed_at)
    WHERE memory_type = 'turn' AND status = 'active' AND project_id IS NULL
      AND ((metadata->>'app_id') = 'circles' OR (metadata->>'app_id') IS NULL)
      AND (metadata->'source_context'->>'kind') IS DISTINCT FROM 'thread';
CREATE INDEX idx_entity_memory_original_replies
    ON entity_memory (entity_id, (COALESCE(landed_at, created_at)))
    WHERE memory_type = 'turn' AND status = 'active' AND project_id IS NULL
      AND ((metadata->>'app_id') = 'circles' OR (metadata->>'app_id') IS NULL)
      AND (metadata->'source_context'->>'kind') IS DISTINCT FROM 'thread'
      AND (metadata->>'role') = 'assistant'
      AND COALESCE(metadata->>'source_kind', '') NOT IN ('thread_marker', 'connector_pick', 'index_pick');

-- ============================================================
-- SESSIONS
-- ============================================================

CREATE TABLE entity_sessions (
    session_id      VARCHAR(36) PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entity_registry(id) ON DELETE CASCADE,
    operator_id     INTEGER,               -- NULL on personal computer
    resolved_weights JSONB,
    collision_strategy VARCHAR(30),
    collision_info  JSONB,
    system_prompt   TEXT NOT NULL,
    interaction_context VARCHAR(50),
    messages        JSONB DEFAULT '[]',
    message_count   INTEGER DEFAULT 0,
    total_tokens    INTEGER DEFAULT 0,
    status          VARCHAR(20) DEFAULT 'active'
                    CHECK (status IN ('active','idle','stale','closed','expired','ended')),
    created_at      TIMESTAMPTZ DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    channel         TEXT DEFAULT 'chat',
    user_id         TEXT,
    last_activity_at TIMESTAMPTZ DEFAULT now(),
    closed_at       TIMESTAMPTZ,
    close_reason    TEXT,
    ephemeral       BOOLEAN DEFAULT false
);

CREATE INDEX idx_entity_sessions_entity ON entity_sessions (entity_id);
CREATE INDEX idx_entity_sessions_status ON entity_sessions (status);
CREATE INDEX idx_sessions_entity_channel_status ON entity_sessions (entity_id, channel, status, last_activity_at DESC);


-- ============================================================
-- CONTEXT STORE
-- ============================================================

CREATE TABLE entity_context (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entity_registry(id) ON DELETE CASCADE,
    file_path       VARCHAR(500) NOT NULL,
    content_type    VARCHAR(50) DEFAULT 'text/plain',
    content         TEXT,
    metadata        JSONB,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (entity_id, file_path)
);

CREATE INDEX idx_entity_context_entity ON entity_context (entity_id);

-- ============================================================
-- PERMISSIONS
-- ============================================================

CREATE TABLE entity_permissions (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entity_registry(id) ON DELETE CASCADE,
    domain          VARCHAR(30) NOT NULL
                    CHECK (domain IN ('tool','data','action','network','hipaa','financial')),
    permission_key  VARCHAR(100) NOT NULL,
    granted         BOOLEAN DEFAULT true,
    scope           JSONB,
    granted_by      VARCHAR(100) NOT NULL,
    reason          TEXT,
    expires_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    revoked_by      VARCHAR(100),
    revoked_reason  TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (entity_id, domain, permission_key)
);

CREATE INDEX idx_entity_permissions_entity ON entity_permissions (entity_id);
CREATE INDEX idx_entity_permissions_domain ON entity_permissions (domain);

-- ============================================================
-- SKILLS / FUNCTIONS
-- ============================================================







-- ============================================================
-- TASKS
-- ============================================================

CREATE TABLE entity_tasks (
    task_id         SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entity_registry(id),
    title           TEXT NOT NULL,
    description     TEXT,
    task_type       TEXT NOT NULL DEFAULT 'research',
    priority        TEXT NOT NULL DEFAULT 'normal'
                    CHECK (priority IN ('low','normal','high','urgent')),
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','in_progress','blocked','completed','failed','cancelled')),
    result          JSONB,
    error           TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    depends_on      INTEGER[] DEFAULT '{}',
    created_by      TEXT NOT NULL DEFAULT 'entity',
    session_id      TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    due_at          TIMESTAMPTZ,
    assigned_to     TEXT,
    project_task_id INTEGER
);

CREATE INDEX idx_entity_tasks_entity_status ON entity_tasks (entity_id, status);
CREATE INDEX idx_entity_tasks_priority ON entity_tasks (priority, created_at);
-- v216: a project's tasks by the time the projects list's last activity takes
-- from each (server/routes/projects.py _LAST_ACTIVITY_SQL). Same definition as
-- migrations/v216_project_list_reads.sql.
CREATE INDEX idx_entity_tasks_project
    ON entity_tasks (entity_id, (result->>'project_id'),
                     (COALESCE(completed_at, started_at, created_at)))
    WHERE (result->>'project_id') IS NOT NULL;

-- ============================================================
-- PROJECTS V2 — Governed Project Execution Engine
-- ============================================================
-- Hierarchy: project → phases → steps
-- Each step: spec_model → execute_model → check (self/model/human)
-- Every action produces a provenance node in a DAG
-- Model routing from codebook competence + user preference

-- Project folders (v130): the grouping layer ABOVE status. A
-- project sits in exactly one folder or none (folder_id NULL = unfiled);
-- folders are flat; a folder holds archived and active projects together;
-- deleting a folder sends its projects back to unfiled, never deletes them.
-- Declared here so a fresh entity matches a long-lived one (the same
-- mismatch `config` and `repo` below record); migrations/v130 adds it to
-- existing databases.
CREATE TABLE project_folders (
    id         SERIAL PRIMARY KEY,
    entity_id  INTEGER NOT NULL,
    name       TEXT NOT NULL,
    color      TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX idx_project_folders_entity_name
    ON project_folders (entity_id, lower(name));

CREATE TABLE projects (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'planning'
                    CHECK (status IN ('planning','active','review','complete','archived')),
    -- v130: the folder this project is filed in; NULL = unfiled. See
    -- project_folders above.
    folder_id       INTEGER REFERENCES project_folders(id) ON DELETE SET NULL,
    -- v173: the project's place in its bucket, set by dragging it in the
    -- Workbench rail; NULL = never placed (sorts after, newest activity first).
    sort_order      INTEGER,
    -- v131: the project index's stored embedding (REAL[] at whatever dim the
    -- entity's model produces; model + hash stamp mark it stale). Written by
    -- the search route's background refresh, only when the host has an
    -- embedding model configured. See migrations/v131.
    search_embedding       REAL[],
    search_embedding_model TEXT,
    search_embedding_hash  TEXT,
    routing_config  JSONB DEFAULT '{}',
    -- Per-project settings bag (distinct from routing_config, which is the
    -- per-domain model routing). Present on long-lived databases as drift and
    -- declared here so a fresh entity matches; migrations/
    -- v109_projects_config.sql backfills the older ones.
    config          JSONB NOT NULL DEFAULT '{}',
    -- Repo-per-project (v081): the gitea full_name (owner/name) this project
    -- builds into. NULL = gitea wasn't connected at creation; the build
    -- backfills it via ensure_project_repo. Declared here for the same
    -- reason `config` above is — migrations/v081_project_repo.sql adds it to
    -- existing databases, and without this nothing creates it on a FRESH
    -- one, so a new entity's schema would not match a long-lived one.
    repo            TEXT,
    -- Store-lineage provenance: when a project is created BY
    -- FORKING an installed store app, this records {"app_id","version"} AT
    -- FORK TIME — the publish lane relays it as the flavor's parent edge
    -- and never invents it later. A project forked from another project's
    -- Code Repository records {"repo","commit"} here instead (v191's design,
    -- projects.fork_repo_for_project). NULL = not a fork. Declared here for
    -- fresh entities; migrations/v116_projects_forked_from.sql backfills
    -- existing databases (all NULL — no fork flow existed before this).
    forked_from     JSONB,
    -- Declared here so a fresh entity matches a long-lived one;
    -- migrations/v075_artifacts.sql adds them to existing databases. The
    -- design-system column that sat here went with the asset catalog
    -- (migrations/v206).
    kind            TEXT NOT NULL DEFAULT 'project'
                    CONSTRAINT projects_kind_check CHECK (kind IN ('project','artifact')),
    disposable      BOOLEAN NOT NULL DEFAULT false,
    -- v215: what the Workbench's tabs read. seen_at is when the person last
    -- had the project's page open (it only moves forward); approvals_waiting
    -- is how many of its Code Approvals waited for the person at the last
    -- forge read, approvals_checked_at when that read was. NULL = never.
    -- migrations/v215_project_page_marks.sql adds them to existing databases.
    seen_at         TIMESTAMPTZ,
    approvals_waiting INTEGER
                    CONSTRAINT projects_approvals_waiting_check
                    CHECK (approvals_waiting IS NULL OR approvals_waiting >= 0),
    approvals_checked_at TIMESTAMPTZ,
    -- v217: the turn tokens of the replies the person watched arrive on the
    -- page, newest last, at most 32. A reply among them never lights the
    -- page's tab, however late its row is written.
    -- migrations/v217_project_seen_turns.sql adds it to existing databases.
    seen_turns      TEXT[]
                    CONSTRAINT projects_seen_turns_check
                    CHECK (seen_turns IS NULL
                           OR (cardinality(seen_turns) <= 32
                               AND array_position(seen_turns, NULL) IS NULL)),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_projects_entity ON projects (entity_id, status);
CREATE INDEX idx_projects_folder ON projects (entity_id, folder_id);
CREATE INDEX idx_projects_entity_kind ON projects (entity_id, kind);
-- One live project per Code Repository (migrations/v191), ticket projects
-- aside: they work in a shared repository on branches of their own, and no
-- two live projects share a branch of one repository (migrations/v199).
CREATE UNIQUE INDEX uq_projects_live_repo
    ON projects (entity_id, lower(repo))
    WHERE repo IS NOT NULL AND status <> 'archived'
      AND NOT (COALESCE(config, '{}'::jsonb) ? 'ticket'
               OR COALESCE(config, '{}'::jsonb) ? 'support_ticket');
CREATE UNIQUE INDEX uq_projects_live_repo_branch
    ON projects (entity_id, lower(repo), (config->>'work_branch'))
    WHERE repo IS NOT NULL AND status <> 'archived'
      AND COALESCE(config, '{}'::jsonb) ? 'work_branch';

-- A project's MODULES (a directory each of its one
-- tree, <project>/<module>/) and their VERSIONS (landed runs, a commit each;
-- the current one is a pointer). Keep in lockstep with
-- migrations/v168_project_modules_and_versions.sql, which explains the columns.
CREATE TABLE project_modules (
    id                  SERIAL PRIMARY KEY,
    project_id          INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name                TEXT NOT NULL
                        CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,39}$'),
    target              TEXT NOT NULL
                        CHECK (target IN ('private_app','public_site','store_app','component')),
    validation          TEXT NOT NULL
                        CHECK (validation IN ('checks','proof','review')),
    description         TEXT NOT NULL DEFAULT '',
    current_version_id  INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);

CREATE TABLE module_versions (
    id          SERIAL PRIMARY KEY,
    module_id   INTEGER NOT NULL REFERENCES project_modules(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    run_id      INTEGER,
    commit_sha  TEXT,
    source      TEXT NOT NULL CHECK (source IN ('build','adopt')),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Set when an undo moves the pointer off this version (or clears it);
    -- undo never lands on an undone version. Cleared when the pointer is
    -- moved back onto it by hand.
    undone_at   TIMESTAMPTZ,
    UNIQUE (module_id, number)
);

CREATE INDEX idx_module_versions_module ON module_versions (module_id, number DESC);

ALTER TABLE project_modules
    ADD CONSTRAINT project_modules_current_version_fk
    FOREIGN KEY (current_version_id) REFERENCES module_versions(id) ON DELETE SET NULL;

CREATE TABLE project_phases (
    id              SERIAL PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    phase_number    INTEGER NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT DEFAULT '',
    spec            TEXT,
    -- 'failed'/'archived' from v108/v110, declared here too (otherwise the
    -- CHECK has the pre-v108 set on a fresh entity until migrations run).
    status          TEXT NOT NULL DEFAULT 'planning'
                    CONSTRAINT project_phases_status_check
                    CHECK (status IN ('planning','speccing','ready','in_progress','review','complete','failed','archived')),
    due_at          TIMESTAMPTZ,
    -- v124: the phase's declared proof, its approval gate, and the approval.
    acceptance      JSONB,
    gate            TEXT NOT NULL DEFAULT 'none'
                    CONSTRAINT project_phases_gate_check CHECK (gate IN ('none','user')),
    approved_at     TIMESTAMPTZ,
    -- v168: the module this phase builds; NULL = the project's root app.
    module_id       INTEGER REFERENCES project_modules(id) ON DELETE SET NULL,
    -- v168: how this phase is proven (checks | proof | review); NULL = written
    -- before v168.
    validation      TEXT
                    CONSTRAINT project_phases_validation_check
                    CHECK (validation IS NULL OR validation IN ('checks','proof','review')),
    -- v175: the phases this one waits for (NULL = the one before it in its
    -- module), the engine it builds on (NULL = the build's), and the merge
    -- conflict that kept its run from landing (NULL = none).
    depends_on      INTEGER[],
    harness         TEXT,
    model           TEXT,
    integration_error TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, phase_number)
);

CREATE INDEX idx_project_phases_project ON project_phases (project_id, status);

CREATE TABLE phase_steps (
    id              SERIAL PRIMARY KEY,
    phase_id        INTEGER NOT NULL REFERENCES project_phases(id) ON DELETE CASCADE,
    step_number     INTEGER NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT DEFAULT '',
    domain_tag      VARCHAR(50),
    spec_model      VARCHAR(100),
    execute_model   VARCHAR(100),
    check_mode      VARCHAR(20) DEFAULT 'self'
                    CHECK (check_mode IN ('self','model','human')),
    check_model     VARCHAR(100),
    model_override  VARCHAR(100),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','speccing','implementing','checking','done','failed')),
    context         JSONB DEFAULT '{}',
    depends_on      INTEGER[] DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (phase_id, step_number)
);

CREATE INDEX idx_phase_steps_phase ON phase_steps (phase_id, status);
CREATE INDEX idx_phase_steps_domain ON phase_steps (domain_tag);

CREATE TABLE step_provenance (
    id              SERIAL PRIMARY KEY,
    step_id         INTEGER NOT NULL REFERENCES phase_steps(id) ON DELETE CASCADE,
    parent_node_id  INTEGER REFERENCES step_provenance(id),
    actor           VARCHAR(100) NOT NULL,
    action          VARCHAR(30) NOT NULL
                    CHECK (action IN ('spec','implement','review','approve','reject','revise')),
    artifact_path   TEXT,
    artifact_summary TEXT,
    input_ref       TEXT,
    tokens_used     INTEGER DEFAULT 0,
    cost_usd        NUMERIC(10,6) DEFAULT 0,
    model_used      VARCHAR(100),
    elapsed_ms      INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_step_provenance_step ON step_provenance (step_id);
CREATE INDEX idx_step_provenance_parent ON step_provenance (parent_node_id);
CREATE INDEX idx_step_provenance_actor ON step_provenance (actor);

CREATE TABLE project_routing (
    id              SERIAL PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    domain          VARCHAR(50) NOT NULL,
    preferred_model VARCHAR(100) NOT NULL,
    reason          TEXT,
    set_by          VARCHAR(30) NOT NULL DEFAULT 'codebook'
                    CHECK (set_by IN ('user','codebook','benchmark')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, domain)
);

CREATE INDEX idx_project_routing_project ON project_routing (project_id);

-- ============================================================
-- CEIGAS DOMAIN REGISTRY (was migrations/v018, folded in here)
-- entity_domain_access is the capability gate's store: the Host's tool bits
-- are derived from its rows (runtime/meta_tool_caps.py). domain_registry is
-- its foreign-key parent and holds the domain names. The domain controller's
-- decision engine and its tables (domain_synapse, domain_signal_log,
-- domain_signal_rollup, trust_ledger) are retired;
-- migrations/v238_domain_engine_retired.sql drops them on a live Host, and
-- on a fresh database after v019 re-creates them for the chain.
-- ============================================================

CREATE TABLE domain_registry (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    brain_analog TEXT,
    description TEXT,
    domain_group TEXT NOT NULL,
    services JSONB DEFAULT '[]'::jsonb,
    ports JSONB DEFAULT '[]'::jsonb,
    health_probe TEXT,
    tier_access_defaults JSONB DEFAULT '{}'::jsonb,
    nt_sensitivity JSONB DEFAULT '{}'::jsonb,
    status TEXT DEFAULT 'active'
        CHECK (status IN ('active', 'degraded', 'offline', 'maintenance')),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE entity_domain_access (
    id SERIAL PRIMARY KEY,
    entity_id INTEGER NOT NULL,
    domain_id TEXT NOT NULL REFERENCES domain_registry(id),
    access_level TEXT NOT NULL DEFAULT 'none'
        CHECK (access_level IN ('none', 'read', 'read_write', 'execute', 'admin')),
    scope_constraints JSONB DEFAULT '{}'::jsonb,
    trust_floor FLOAT DEFAULT 0.0,
    granted_by TEXT,
    grant_reason TEXT,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(entity_id, domain_id)
);

-- ============================================================
-- CONFIG VERSIONS (memory network protocol)
-- ============================================================

-- Monotonic sequence per entity-scoped config field, tracked on both
-- EME (local copy) and PME (master). Pod applies push only when the
-- incoming sequence > the local sequence; idempotent re-delivery.
CREATE TABLE config_versions (
    entity_id          INTEGER NOT NULL,
    field_path         TEXT NOT NULL,            -- e.g. "behavioral_params.inference_model"
    sequence           BIGINT NOT NULL DEFAULT 1,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by         TEXT,
    PRIMARY KEY (entity_id, field_path)
);

-- Seed the domain registry: every domain an entity_domain_access row names.
INSERT INTO domain_registry (id, name, brain_analog, domain_group, description) VALUES
    ('core.entity',      'Entity',         'self',              'core', 'The entity itself — source of all internal thought and action'),
    ('core.cognitive',   'Cognitive',      'prefrontal_cortex', 'core', 'Inference / reasoning surface'),
    ('core.memory',      'Memory',         'hippocampus',       'core', 'Long-term + working memory'),
    ('core.governance',  'Governance',     'amygdala',          'core', 'Constitutional rules, kill switch, audit'),
    ('bots.execution',   'Bot Execution',  'motor_cortex',      'bots', 'Skill / bot dispatch surface'),
    ('infra.codebase',   'Codebase',       'parietal_cortex',   'infra', 'Source-code reads / edits / builds'),
    ('infra.oracle',     'Oracle',         'occipital_cortex',  'infra', 'Memory pipeline manager'),
    ('infra.database',   'Database',       'thalamus',          'infra', 'Direct DB access'),
    ('infra.network',    'Network',        'cerebellum',        'infra', 'Network ops / DNS / routing'),
    ('container.relay',  'Container Relay','brain_stem',        'core', 'Container-to-bridge tunnel'),
    ('device.filesystem','Filesystem',     'motor_cortex',      'device','Local filesystem ops'),
    ('external.mcp',     'External MCP',   'sensory_cortex',    'io',   'External MCP clients (Claude Desktop, Cursor, etc.)'),
    ('external.operator','External Operator','sensory_cortex',  'io',   'Operator''s cseries-gateway endpoint'),
    ('synaptive.shape',  'Synaptive Shape','prefrontal_cortex', 'core', 'Synaptive identity surface for external chats')
ON CONFLICT (id) DO NOTHING;

-- Per-entity self-access grants: the capability bits of a new Host's own
-- tools come from these rows (zero rows, zero bits). Reads the entity's id
-- from entity_registry (each pod has exactly one row).
-- infra.codebase is NOT a self grant: it put the platform's
-- source, image, service and bridge-DaemonSet tools in every Host's tool
-- list. The AUTHORITY is the central entity_domain_access row, which the
-- bridge enforces per op (the bridge adapter's _refuse_platform_op).
-- The LIST still reads this pod table
-- (runtime/tools.py, the infra.codebase=admin probe), so a Host that should
-- see those tools needs a row here as well. No seed writes it: the kernel
-- copies the central level into it (server/platform_grant_sync.py).
INSERT INTO entity_domain_access (entity_id, domain_id, access_level, granted_by, grant_reason)
SELECT
    er.id,
    d.domain_id,
    d.access_level,
    'init-db.sql',
    'self — every entity needs to invoke its own cognition + memory'
FROM (SELECT id FROM entity_registry LIMIT 1) er
CROSS JOIN (VALUES
    ('core.entity',     'admin'),
    ('core.cognitive',  'execute'),
    ('core.memory',     'admin'),
    ('core.governance', 'read'),
    ('bots.execution',  'execute'),
    ('container.relay', 'read_write'),
    ('external.operator','read_write')
) AS d(domain_id, access_level)
ON CONFLICT (entity_id, domain_id) DO UPDATE
  SET access_level = EXCLUDED.access_level
  WHERE entity_domain_access.access_level = 'none';

-- ============================================================
-- LEGACY TABLES KEPT FOR THE MIGRATION CHAIN
-- ============================================================
-- Nothing in the kernel reads or writes these two. They are created here
-- only so the
-- shipped migrations that INSERT into or ALTER them (v028, v029, v039;
-- v085, v086) replay on a fresh database (v028's own CREATE IF NOT EXISTS of
-- neurotransmitter_systems lacks entity_id, so that table is kept too); migrations/v235_one_memory.sql
-- drops them at the end of the chain. Never add a reader.

CREATE TABLE entity_behavioral_log (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entity_registry(id) ON DELETE CASCADE,
    event_type      VARCHAR(30) NOT NULL
                    CHECK (event_type IN ('drift','violation','reset','debug_start',
                        'debug_complete','restore','suspend','escalation',
                        'envelope_breach','bot_execution','tool_result',
                        'skill_practiced','skill_learned','skill_outdated',
                        'codebook_drift','codebook_reset','codebook_update')),
    weights_before  JSONB,
    weights_after   JSONB,
    drift_from_seed JSONB,
    trigger_source  TEXT,
    trigger_detail  TEXT,
    severity        VARCHAR(20) DEFAULT 'info'
                    CHECK (severity IN ('info','warning','critical','emergency')),
    auto_action     VARCHAR(30)
                    CHECK (auto_action IN ('none','warn','reset_dimension','reset_full',
                        'suspend','debug','escalate','ceigas_block')),
    action_result   JSONB,
    debug_snapshot  JSONB,
    debug_diagnosis TEXT,
    resolved        BOOLEAN DEFAULT false,
    resolved_at     TIMESTAMPTZ,
    resolved_by     VARCHAR(100),
    resolution_notes TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE neurotransmitter_systems (
    id              SERIAL PRIMARY KEY,
    system_name     VARCHAR(50) NOT NULL,
    analog_name     VARCHAR(50) NOT NULL,
    description     TEXT NOT NULL,
    effect          TEXT NOT NULL,
    baseline_level  DOUBLE PRECISION DEFAULT 0.5,
    current_level   DOUBLE PRECISION DEFAULT 0.5,
    min_level       DOUBLE PRECISION DEFAULT 0.0,
    max_level       DOUBLE PRECISION DEFAULT 1.0,
    decay_rate      DOUBLE PRECISION DEFAULT 0.01,
    target_codebooks TEXT[],
    modulation_rules JSONB NOT NULL,
    requires_human_approval BOOLEAN DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT now(),
    entity_id       INTEGER REFERENCES entity_registry(id)
);

CREATE INDEX idx_nt_entity ON neurotransmitter_systems (entity_id);
CREATE UNIQUE INDEX idx_nt_system_entity ON neurotransmitter_systems (system_name, entity_id);
CREATE UNIQUE INDEX idx_nt_system_null ON neurotransmitter_systems (system_name) WHERE entity_id IS NULL;

CREATE TABLE codebook_registry (
    id              SERIAL PRIMARY KEY,
    codebook_name   VARCHAR(100) NOT NULL UNIQUE,
    brain_region    VARCHAR(100) NOT NULL,
    brain_system    VARCHAR(50) NOT NULL,
    hierarchy_level INTEGER NOT NULL DEFAULT 0,
    description     TEXT NOT NULL,
    input_signals   JSONB NOT NULL,
    output_signals  JSONB NOT NULL,
    processing_type VARCHAR(50) NOT NULL
                    CHECK (processing_type IN ('pattern_match','association','inhibition',
                        'integration','generation','modulation','prediction','evaluation',
                        'sequencing','routing')),
    processing_rules JSONB NOT NULL,
    governed_params JSONB NOT NULL,
    neurotransmitter_profile JSONB,
    depends_on      TEXT[],
    feeds_into      TEXT[],
    status          VARCHAR(20) DEFAULT 'draft'
                    CHECK (status IN ('draft','training','active','archived')),
    version         INTEGER DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT now(),
    trained_at      TIMESTAMPTZ,
    activated_at    TIMESTAMPTZ
);

CREATE INDEX idx_codebook_region ON codebook_registry (brain_region);
CREATE INDEX idx_codebook_system ON codebook_registry (brain_system);
CREATE INDEX idx_codebook_level ON codebook_registry (hierarchy_level);
CREATE INDEX idx_codebook_status ON codebook_registry (status);

CREATE TABLE entity_interactions (
    id                  SERIAL PRIMARY KEY,
    entity_id           INTEGER NOT NULL REFERENCES entity_registry(id) ON DELETE CASCADE,
    operator_id         INTEGER,
    interaction_type    VARCHAR(30) NOT NULL
                        CHECK (interaction_type IN ('conversation','tool_use','api_call',
                            'voice_call','email','sms','internal')),
    summary             TEXT,
    tokens_used         INTEGER DEFAULT 0,
    duration_seconds    DOUBLE PRECISION,
    active_weights      JSONB,
    collision_strategy  VARCHAR(30),
    satisfaction_signal DOUBLE PRECISION,
    goal_progress       JSONB,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_entity_interactions_entity ON entity_interactions (entity_id);
CREATE INDEX idx_entity_interactions_type ON entity_interactions (interaction_type);

-- ============================================================
-- SEED DATA
-- ============================================================

-- Entity 1 — populated during provisioning. Placeholder with safe defaults.
-- Scale: 0.0-2.0, baseline 1.0. Envelope bounds [min, max] per dimension.
INSERT INTO entity_registry (
    entity_type, display_name, master_directive, seed_weights, current_weights,
    envelope_bounds, status, activated_at
) VALUES (
    'personal',
    'Host',
    '',
    '{"warmth":1.0,"directness":1.0,"humor":1.0,"formality":1.0,"curiosity":1.0,"assertiveness":1.0,"patience":1.0,"vulnerability":1.0,"energy":1.0,"depth":1.0}',
    '{"warmth":1.0,"directness":1.0,"humor":1.0,"formality":1.0,"curiosity":1.0,"assertiveness":1.0,"patience":1.0,"vulnerability":1.0,"energy":1.0,"depth":1.0}',
    '{"warmth":[0.1,2.0],"directness":[0.1,2.0],"humor":[0.0,2.0],"formality":[0.0,2.0],"curiosity":[0.1,2.0],"assertiveness":[0.1,2.0],"patience":[0.1,2.0],"vulnerability":[0.0,2.0],"energy":[0.1,2.0],"depth":[0.1,2.0]}',
    'active', now()
);

-- ============================================================
-- CODEBOOK RECOMMENDATIONS — retired
-- ============================================================
-- The governed corrections→recommendation→approve flow (producer:
-- mastercode/personality_recommender.py, deleted) is replaced by the
-- constitution-side rule promoter + direct user sliders (PUT
-- /api/brain/personality/weights). migrations/v094_restoration_two_systems.sql
-- DROPs this table for existing entities; removed here too so a freshly
-- provisioned entity never creates it in the first place (would otherwise
-- be a table with zero readers/writers from birth — caught live by
-- scripts/audit_orphan_tables.py).

-- ============================================================
-- RATE LIMITING (per-entity daily API budget)
-- ============================================================


-- ── Performance indexes (added for scale) ──────────────────

-- Session cleanup scan: session_handler.py scans ALL sessions every 15 min.
-- Composite on (status, last_activity_at) lets the cleanup query skip closed sessions instantly.
CREATE INDEX IF NOT EXISTS idx_entity_sessions_status_activity
ON entity_sessions(status, last_activity_at);

-- Context store prefix search: context_store.py uses LIKE on file_path.
-- Composite on (entity_id, file_path) supports btree prefix matching.
CREATE INDEX IF NOT EXISTS idx_entity_context_entity_path
ON entity_context(entity_id, file_path);

-- Memory embedding status: oracle app filters by has_embedding.
-- Partial expression index avoids scanning unembedded rows.
CREATE INDEX IF NOT EXISTS idx_entity_memory_embedding
ON entity_memory(entity_id, (embedding IS NOT NULL));

-- Behavioral log: monitor app queries recent events per entity.
-- Same pattern — composite beats two single-column index merges.
CREATE INDEX IF NOT EXISTS idx_entity_behavioral_log_entity_created
ON entity_behavioral_log(entity_id, created_at DESC);

-- ============================================================
-- BENCHMARK: Capability measurement with codebook training loop
-- ============================================================




-- ── Behavioral corrections (constitutional enforcement layer) ────────
CREATE TABLE IF NOT EXISTS behavioral_corrections (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL,
    correction      TEXT NOT NULL,
    trigger_context TEXT DEFAULT '',
    source_message  TEXT DEFAULT '',
    feedback_type   TEXT NOT NULL DEFAULT 'correction',
    confidence      FLOAT DEFAULT 0.5,
    severity        TEXT NOT NULL DEFAULT 'gentle',
    times_violated  INTEGER DEFAULT 0,
    active          BOOLEAN DEFAULT true,
    trigger_embedding vector(1024),
    -- Multi-dim trigger columns (v101) — routed by the configured embedding
    -- model's dim via mastercode/correction_embeddings.py, mirroring
    -- entity_memory's scheme. halfvec above 2000d. No vector indexes: the
    -- per-entity correction set is small, sequential scan is fine.
    trigger_embedding_768  vector(768),
    trigger_embedding_2048 halfvec(2048),
    trigger_embedding_2560 halfvec(2560),
    trigger_embedding_4096 halfvec(4096),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    role            VARCHAR(100)
);

CREATE INDEX IF NOT EXISTS idx_behavioral_corrections_entity
    ON behavioral_corrections(entity_id, active, severity);
CREATE INDEX IF NOT EXISTS idx_behavioral_corrections_role
    ON behavioral_corrections (entity_id, role, severity, active);

-- ── Journal (session logs, decisions, research records) ──────────


-- ── Embed jobs (background embedding task queue) ─────────────
CREATE TABLE IF NOT EXISTS embed_jobs (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL,
    job_type        TEXT NOT NULL DEFAULT 'full',
    target          TEXT NOT NULL DEFAULT 'entity_memory',
    status          TEXT NOT NULL DEFAULT 'pending',
    total           INTEGER NOT NULL DEFAULT 0,
    embedded        INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    last_id         INTEGER,
    model           TEXT,
    error           TEXT,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ── Signal queue (raw external signals → judgment → entity_memory) ──
-- The "water tank" between awareness adapters and entity_memory.
-- signal_queue table + indexes + trigger removed alongside the abandoned
-- services/awareness/ pipeline. Synaptive signal() now routes directly
-- through MemoryEngine.extract_and_store, the same path session.py uses
-- for primary chat. The product slot named "feed" is fulfilled by the
-- separate cseries-feed service, not by a per-entity queue.

-- ── User context (uploaded documents, manual context) ────────
CREATE TABLE IF NOT EXISTS user_context (
    id              SERIAL PRIMARY KEY,
    entity_id       INTEGER NOT NULL,
    content         TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT 'manual',
    source_id       TEXT,
    title           TEXT,
    -- Multi-dim content embeddings, mirroring entity_memory. The entity's
    -- active model decides which column its vectors land in (768/1024/2048/
    -- 4096). `embedding` is the 1024d default. embedding_2048/4096 are kept
    -- unindexed (pgvector HNSW caps at 2000d) — same trade-off as
    -- entity_memory.
    embedding       vector(1024),
    embedding_768   vector(768),
    embedding_2048  vector(2048),
    embedding_4096  vector(4096),
    tags            TEXT[] DEFAULT '{}',
    metadata        JSONB DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'raw',
    promoted_to     INTEGER,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_user_context_embedding_768_hnsw
    ON user_context USING hnsw (embedding_768 vector_cosine_ops) WITH (m=16, ef_construction=64);
CREATE INDEX IF NOT EXISTS idx_user_context_embedding_hnsw
    ON user_context USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);

-- ============================================================
-- WORLD MODEL: AGE graph init
-- ============================================================
-- Hypertable conversion of entity_memory is DEFERRED.
-- Timescale requires the partition column (created_at) to be part of every
-- unique index, including PK. entity_memory's PK is (id) only, and the self-
-- FKs (correction_of, corrected_by) reference id directly. Making PK composite
-- (id, created_at) would invalidate those FKs. At realistic per-entity volumes
-- (~50-100 memories/day), a flat indexed table is fine for years.
-- The timescaledb extension stays installed; revisit conversion when single-
-- entity row counts approach hundreds of thousands and chunking would help.

-- ============================================================
-- Synaptive — bearer tokens for external connected chats.
-- ============================================================
-- Tokens authorize an MCP client (Claude Desktop, Cursor, etc.) to call
-- shape() against this user's container. Plaintext is shown to the user
-- once at creation; only the sha256 hash is stored. url_handle is the
-- per-token un-guessable subdomain segment used by the rev-tunnel edge.
SET search_path = public;
CREATE TABLE IF NOT EXISTS mcp_client_tokens (
  id                   SERIAL PRIMARY KEY,
  entity_id            INT NOT NULL,
  name                 TEXT NOT NULL,
  token_hash           BYTEA NOT NULL,
  url_handle           TEXT NOT NULL,
  scopes               TEXT[] NOT NULL DEFAULT ARRAY['shape'],
  -- Operator-class fields (C-Series Gateway).
  -- Mirrors the gateway's own first migration.
  operator_kind        TEXT,        -- bastion | claude_code | claude_desktop | lm_studio | cursor | other
  machine_anchor_id    BIGINT,      -- soft ref to privatae.machine_anchors.anchor_id (cross-DB)
  hardware_fingerprint TEXT,        -- captured at bind; matched on every gateway request
  notes                JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used_at         TIMESTAMPTZ,
  revoked_at           TIMESTAMPTZ
);
-- Idempotent ALTERs so existing entity DBs (predate the operator columns)
-- pick them up on next container restart.
ALTER TABLE mcp_client_tokens ADD COLUMN IF NOT EXISTS operator_kind        TEXT;
ALTER TABLE mcp_client_tokens ADD COLUMN IF NOT EXISTS machine_anchor_id    BIGINT;
ALTER TABLE mcp_client_tokens ADD COLUMN IF NOT EXISTS hardware_fingerprint TEXT;
ALTER TABLE mcp_client_tokens ADD COLUMN IF NOT EXISTS notes                JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS uniq_mcp_token_hash_active
  ON mcp_client_tokens (token_hash) WHERE revoked_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uniq_mcp_handle_active
  ON mcp_client_tokens (url_handle) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_entity
  ON mcp_client_tokens (entity_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_operator_kind
  ON mcp_client_tokens (operator_kind) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_anchor
  ON mcp_client_tokens (machine_anchor_id) WHERE revoked_at IS NULL;

-- ============================================================
-- C-Series Gateway — encrypted backup envelopes + blobs.
-- ============================================================
-- The user's local PG (managed by the gateway daemon on their machine)
-- pushes ciphertext rows here. Hosted side stores ciphertext only; cannot
-- decrypt without the user's passphrase or recovery code.
--
-- Mirrors the gateway's own local_backup migration.
CREATE TABLE IF NOT EXISTS local_backup_envelope (
  envelope_id      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
  kdf_source       TEXT            NOT NULL
      CHECK (kdf_source IN ('passphrase','recovery_code')),
  wrapped_dek      BYTEA           NOT NULL,
  wrap_nonce       BYTEA           NOT NULL,
  wrap_auth_tag    BYTEA           NOT NULL,
  wrap_algo        TEXT            NOT NULL DEFAULT 'aes-256-gcm',
  kdf_algo         TEXT            NOT NULL DEFAULT 'argon2id',
  kdf_salt         BYTEA           NOT NULL,
  kdf_params       JSONB           NOT NULL,
  is_active        BOOLEAN         NOT NULL DEFAULT TRUE,
  created_at       TIMESTAMPTZ     NOT NULL DEFAULT now(),
  superseded_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_backup_envelope_active
  ON local_backup_envelope (kdf_source) WHERE is_active = TRUE;

CREATE TABLE IF NOT EXISTS local_backup_blob (
  blob_id          BIGSERIAL       PRIMARY KEY,
  resource_kind    TEXT            NOT NULL,
  resource_pk      TEXT            NOT NULL,
  nonce            BYTEA           NOT NULL,
  ciphertext       BYTEA           NOT NULL,
  auth_tag         BYTEA           NOT NULL,
  enc_algo         TEXT            NOT NULL DEFAULT 'aes-256-gcm',
  plaintext_hash   BYTEA           NOT NULL,
  row_version      BIGINT          NOT NULL,
  created_at       TIMESTAMPTZ     NOT NULL DEFAULT now(),
  superseded_at    TIMESTAMPTZ,
  UNIQUE (resource_kind, resource_pk, row_version)
);
CREATE INDEX IF NOT EXISTS idx_local_backup_blob_resource
  ON local_backup_blob (resource_kind, resource_pk, row_version DESC);
CREATE INDEX IF NOT EXISTS idx_local_backup_blob_active
  ON local_backup_blob (resource_kind, created_at DESC)
  WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_local_backup_blob_plaintext_hash
  ON local_backup_blob (plaintext_hash);

-- ============================================================
-- Credential vault — identity-scoped sealed envelopes.
-- ============================================================
-- Each credential is sealed (server/vault/crypto.py: a per-secret DEK wrapped to
-- recipient X25519 public keys) and owned by exactly one identity: host:<entity_id>,
-- user:<user_id>, or platform. The platform stores ciphertext + public keys only;
-- host/user private keys never leave their own space, so it is structurally unable
-- to read host/user-owned secrets. Mirrors the ensure-schema in server/vault/.
CREATE TABLE IF NOT EXISTS vault_envelope (
  id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_identity TEXT         NOT NULL,
  logical_name   TEXT         NOT NULL,
  cred_type      TEXT         NOT NULL,
  envelope       JSONB        NOT NULL,
  kek_version    INT          NOT NULL DEFAULT 1,
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  superseded_at  TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vault_envelope_active
  ON vault_envelope (owner_identity, logical_name) WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_vault_envelope_owner
  ON vault_envelope (owner_identity) WHERE superseded_at IS NULL;

-- Recipient public keys for non-local identities (users / platform). PUBLIC keys
-- only — the matching private keys live in those identities' own space.
CREATE TABLE IF NOT EXISTS vault_identity_pubkey (
  identity    TEXT PRIMARY KEY,
  pubkey      BYTEA NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vault_audit (
  id             BIGSERIAL    PRIMARY KEY,
  at             TIMESTAMPTZ  NOT NULL DEFAULT now(),
  actor          TEXT         NOT NULL,
  op             TEXT         NOT NULL,
  owner_identity TEXT,
  logical_name   TEXT,
  detail         JSONB
);
CREATE INDEX IF NOT EXISTS idx_vault_audit_name
  ON vault_audit (owner_identity, logical_name, at DESC);

-- ============================================================
-- OPERATOR AUDIT LOG (gateway-fed, tamper-evident HMAC chain)
-- ============================================================
-- own operator activity from the container. Gateway daemon writes
-- here as it forwards crossings; entity never writes.
-- Mirrors the gateway's own local/001_init migration.

-- ============================================================
-- GITEA PROPOSE AUDIT — the host's fork-and-PR lane
-- ============================================================
-- One row per call to POST /api/gitea/propose (server/routes/gitea_propose.py),
-- refusals included — the nac_mgmt_audit shape. files (JSON paths) + commit
-- make a proposal reconstructable after its fork branch is gone. Mirrored in
-- migrations/v134_gitea_propose_audit.sql for entities provisioned earlier.
CREATE TABLE IF NOT EXISTS gitea_propose_audit (
  id          BIGSERIAL     PRIMARY KEY,
  entity_id   INTEGER,
  op          TEXT          NOT NULL DEFAULT 'gitea:propose',
  repo        TEXT,
  fork        TEXT,
  branch      TEXT,
  pr_number   INTEGER,
  pr_url      TEXT,
  files       JSONB         NOT NULL DEFAULT '[]'::jsonb,
  file_count  INTEGER       NOT NULL DEFAULT 0,
  commit      TEXT,
  outcome     TEXT          NOT NULL,          -- opened | updated | refused | error
  detail      TEXT,
  at          TIMESTAMPTZ   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gitea_propose_audit_repo
  ON gitea_propose_audit (repo, at DESC);
CREATE INDEX IF NOT EXISTS idx_gitea_propose_audit_recent
  ON gitea_propose_audit (at DESC);

-- ============================================================
-- AWARENESS INBOX: Channels + Messages
-- ============================================================
-- User-visible "airport board" surface for the awareness pipeline.
-- One channel per (owner_entity_id, channel_type, source_type, source_id):
-- 40 SolarWinds alerts collapse into one channel with 40 messages,
-- not 40 separate inbox surfaces.



-- ============================================================
-- AWARENESS DISPATCH: Notify state
-- ============================================================
-- High-water mark per dispatcher backend so we don't re-send
-- already-dispatched signals on every run.


-- ============================================================
-- INFERENCE COST LEDGER
-- ============================================================
-- Per-call usage + cost record. Every LLM call site (judge, summarizer,
-- classifier, research probe, topic extractor) writes one row.

CREATE TABLE IF NOT EXISTS inference_cost_log (
    id              BIGSERIAL PRIMARY KEY,
    entity_id       INTEGER,
    source_module   TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    provider        TEXT,
    purpose         TEXT,
    -- NULL = the provider reported no usage for the call (v214).
    tokens_in       INTEGER DEFAULT 0,
    tokens_out      INTEGER DEFAULT 0,
    est_cost_usd    DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    latency_ms      INTEGER,
    metadata        JSONB DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_inference_cost_recent
    ON inference_cost_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inference_cost_entity_recent
    ON inference_cost_log (entity_id, created_at DESC) WHERE entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inference_cost_module_recent
    ON inference_cost_log (source_module, created_at DESC);

-- ============================================================
-- MEMORY PASS LEDGER — is memory still forming?
-- ============================================================
-- One row per turn-consolidator pass, written on EVERY path including the
-- quiet one. Companion to migrations/v117_memory_pass_log.sql (which carries
-- the full rationale); declared here so it is first-class on a fresh entity
-- from second-zero rather than arriving only via the migration loop.
--
-- The load-bearing property: staleness is computed by the READER from
-- max(finished_at), never self-reported. A consolidator loop that dies
-- silently therefore surfaces as STALE instead of as silence — which is the
-- exact failure this exists to end (fact derivation once went dead for 16
-- days, and the evidence was one INFO line below the pod's log level).
CREATE TABLE IF NOT EXISTS memory_pass_log (
    id                   BIGSERIAL   PRIMARY KEY,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at          TIMESTAMPTZ,
    status               TEXT        NOT NULL DEFAULT 'ok',  -- ok | partial | failed
    turns_found          INTEGER     NOT NULL DEFAULT 0,
    turns_marked         INTEGER     NOT NULL DEFAULT 0,
    memories_derived     INTEGER     NOT NULL DEFAULT 0,
    sessions_processed   INTEGER     NOT NULL DEFAULT 0,
    entities_touched     INTEGER     NOT NULL DEFAULT 0,
    entities_unconfigured INTEGER    NOT NULL DEFAULT 0,
    graph_queued         INTEGER     NOT NULL DEFAULT 0,
    graph_processed      INTEGER     NOT NULL DEFAULT 0,
    graph_failed         INTEGER     NOT NULL DEFAULT 0,
    error                TEXT,
    duration_ms          INTEGER,
    metadata             JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_memory_pass_recent
    ON memory_pass_log (finished_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_memory_pass_ok
    ON memory_pass_log (finished_at DESC) WHERE status = 'ok';
CREATE INDEX IF NOT EXISTS idx_memory_pass_problems
    ON memory_pass_log (started_at DESC) WHERE status <> 'ok';

-- ============================================================
-- HARNESS USAGE LEDGER (ComputeView)
-- ============================================================
-- One row per harness run: tokens (parsed/sdk/unknown), metered compute time,
-- our compute-fee cost, and whether the user SHIPPED that harness's code
-- (committed). Powers cross-harness usage/spend + cost-per-accepted-commit.
-- Written by review.py at run completion; committed flag set at propose_commit.
CREATE TABLE IF NOT EXISTS harness_usage (
    id                BIGSERIAL PRIMARY KEY,
    session_id        TEXT,
    project_id        INTEGER,
    harness           TEXT NOT NULL,
    model             TEXT,
    run_kind          TEXT,
    tokens_total      INTEGER NOT NULL DEFAULT 0,
    token_source      TEXT NOT NULL DEFAULT 'unknown',
    wall_clock_s      DOUBLE PRECISION NOT NULL DEFAULT 0,
    compute_s         DOUBLE PRECISION,
    cost_microdollars BIGINT NOT NULL DEFAULT 0,
    cost_method       TEXT NOT NULL DEFAULT 'unpriced',
    committed         BOOLEAN NOT NULL DEFAULT false,
    committed_files   INTEGER NOT NULL DEFAULT 0,
    ok                BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_harness_usage_recent  ON harness_usage (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_harness_usage_harness ON harness_usage (harness, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_harness_usage_session ON harness_usage (session_id);

-- ============================================================
-- ============================================================
-- The entity treats benchmarks as projects, runs them, classifies failures,
-- adjusts codebooks, reruns. Same loop should run on any user task.
-- Snapshots enable rollback + delta attribution per cycle.






-- ═══════════════════════════════════════════════════════════════════════
-- Migration: entity_tickets + decision_patterns
--
-- These are the entity-private side of "Entity as Endpoint":
--   * tickets — escalations the entity needs the handler to decide on.
--     Each ticket has a snapshot sitrep, a small set of options, and
--     becomes a decision_pattern when the handler picks "and-similar".
--   * decision_patterns — handler-approved rules that let the entity
--     auto-resolve matching future requests without escalating.
--
-- Tickets live in the container's local PG (private to this entity).
-- Decision patterns live in the container's local PG (this entity's
-- learned rules). Circle membership / share scopes live in the platform database.
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS entity_tickets (
    id              SERIAL      PRIMARY KEY,
    ticket_uuid     UUID        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    entity_id       INTEGER     NOT NULL,

    -- Where did this come from?
    --   circle_dispatch — entity escalated from a @mention in a circle
    --   email           — email rule fed the entity a message, it asked
    --   slack | manual  — future ingestion paths
    source          TEXT        NOT NULL,
    source_ref      TEXT,                  -- email msg-id, circle thread, etc.
    circle_id       INTEGER,               -- nullable; tickets without circles go to global queue
    requester       JSONB       NOT NULL DEFAULT '{}'::jsonb,
                                            -- {kind, handle, display_name}

    -- The brief and the options. Sitrep is plain-text dense English.
    -- Options is an array of {id, label, consequence, recommend, pattern_template}.
    sitrep          TEXT        NOT NULL DEFAULT '',
    options         JSONB       NOT NULL DEFAULT '[]'::jsonb,
    context         JSONB       NOT NULL DEFAULT '{}'::jsonb,
                                            -- pulled memory, requirements, attachments

    status          TEXT        NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'decided', 'auto_resolved', 'expired', 'dismissed')),
    priority        SMALLINT    NOT NULL DEFAULT 50,   -- 0..100, higher = sooner

    -- Decision lifecycle
    decision        JSONB,                  -- {option_id, and_similar, note}
    decided_by      INTEGER,
    decided_at      TIMESTAMPTZ,
    pattern_id      INTEGER,                -- FK to decision_patterns when "and-similar"

    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_tickets_entity_status
    ON entity_tickets (entity_id, status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_circle_status
    ON entity_tickets (circle_id, status) WHERE circle_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tickets_source
    ON entity_tickets (entity_id, source, opened_at DESC);


-- ─────────────────────────────────────────────────────────────────────
-- Decision patterns — handler-crystallized rules
--
-- When the handler picks an option WITH "and-similar-going-forward",
-- the entity records the pattern. Future tickets that match the
-- conditions auto-resolve to the same option without paging the
-- handler. The handler sees a digest of auto-resolved tickets in the
-- Reviews queue's "Recent auto-resolved" tab.
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS decision_patterns (
    id              SERIAL      PRIMARY KEY,
    pattern_uuid    UUID        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    entity_id       INTEGER     NOT NULL,
    circle_id       INTEGER,                -- nullable: global-vs-per-circle pattern

    -- What class of decision does this pattern cover?
    -- Free-form so handler/entity can pick natural taxonomies:
    -- "quote_standard_tier", "share_product_specs", "auto_acknowledge".
    action_class    TEXT        NOT NULL,

    -- Match predicates the entity grades incoming tickets against.
    -- Free-form so the entity can encode whatever it inferred from the
    -- approved instance:
    --   {requester_kind: "prospect", ticket_kind: "quote_request",
    --    amount_max_usd: 5000, scope_overlap_required: ["product_specs"]}
    conditions      JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- The decision to apply when conditions match.
    decision_template JSONB     NOT NULL DEFAULT '{}'::jsonb,
                                            -- {option_id, response_template}

    -- Observability + governance
    source_ticket_id  INTEGER,              -- the ticket that crystallized this pattern
    approved_by       INTEGER     NOT NULL,
    approved_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    observations      INTEGER     NOT NULL DEFAULT 1,  -- ticked up on each auto-resolve
    last_used_at      TIMESTAMPTZ,
    status            TEXT        NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'paused', 'retired')),

    CONSTRAINT decision_pattern_unique_per_class
        UNIQUE (entity_id, circle_id, action_class)
);

CREATE INDEX IF NOT EXISTS idx_patterns_entity_status
    ON decision_patterns (entity_id, status, action_class);
CREATE INDEX IF NOT EXISTS idx_patterns_circle
    ON decision_patterns (circle_id) WHERE circle_id IS NOT NULL;

-- After-decided FK so a ticket points at the pattern it spawned.
-- Postgres doesn't support `ADD CONSTRAINT IF NOT EXISTS`, so guard via
-- pg_constraint lookup.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'entity_tickets_pattern_fk'
    ) THEN
        ALTER TABLE entity_tickets
            ADD CONSTRAINT entity_tickets_pattern_fk
            FOREIGN KEY (pattern_id) REFERENCES decision_patterns(id) ON DELETE SET NULL;
    END IF;
END $$;

COMMENT ON TABLE entity_tickets IS
    'Escalations the entity is asking the handler to decide on. Source can be circle dispatch, email, slack, etc. Always private to the entity.';
COMMENT ON TABLE decision_patterns IS
    'Handler-approved rules. When a ticket matches an active pattern, the entity auto-resolves without escalating.';

-- ============================================================
-- calendar_events + inbox_messages live in the pod (EME)
-- ============================================================
-- The previous home for these two was the platform's central PG (PME). The
-- per-entity rows live on the pod side so the entity is the only reader
-- of its own data. Bridge ops that used to write to central calendar_events
-- / inbox_messages now device.dispatch through to pod handlers — see
-- server/bridge/memory_handlers.py for the receiving side.

CREATE TABLE IF NOT EXISTS calendar_events (
    id                  SERIAL PRIMARY KEY,
    entity_id           INTEGER NOT NULL,
    title               VARCHAR(500) NOT NULL,
    description         TEXT,
    start_time          TIMESTAMPTZ NOT NULL,
    end_time            TIMESTAMPTZ NOT NULL,
    all_day             BOOLEAN DEFAULT FALSE,
    color               VARCHAR(7),
    recurrence          VARCHAR(50),
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    created_by          VARCHAR(20) DEFAULT 'user',
    category            VARCHAR(50) DEFAULT 'personal',
    event_type          TEXT NOT NULL,
    event_at            TIMESTAMPTZ NOT NULL,
    summary             TEXT NOT NULL,
    payload             JSONB DEFAULT '{}'::jsonb,
    source              TEXT NOT NULL,
    status              TEXT NOT NULL,
    cost_usd            NUMERIC(12,6),
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    model_id            TEXT,
    related_message_id  UUID,
    related_artifact_uri TEXT,
    related_project_id  INTEGER,
    ceigas_crossing_id  UUID,
    scheduled_by        TEXT,
    cancelled_reason    TEXT,
    event_id            UUID DEFAULT gen_random_uuid() NOT NULL,
    CONSTRAINT calendar_events_event_type_format
        CHECK (event_type ~ '^[a-z][a-z0-9_]*$'),
    CONSTRAINT calendar_events_status_check
        CHECK (status IN ('scheduled','running','completed','failed','cancelled')),
    CONSTRAINT calendar_events_event_id_key UNIQUE (event_id)
);
CREATE INDEX IF NOT EXISTS idx_calendar_daily_summary
    ON calendar_events (entity_id, event_at) WHERE event_type = 'daily_summary';
CREATE INDEX IF NOT EXISTS idx_calendar_entity_time
    ON calendar_events (entity_id, event_at DESC);
CREATE INDEX IF NOT EXISTS idx_calendar_entity_type
    ON calendar_events (entity_id, event_type, event_at DESC);
CREATE INDEX IF NOT EXISTS idx_calendar_events_category
    ON calendar_events (entity_id, category);
CREATE INDEX IF NOT EXISTS idx_calendar_events_entity_time
    ON calendar_events (entity_id, start_time, end_time);
CREATE INDEX IF NOT EXISTS idx_calendar_scheduled
    ON calendar_events (event_at) WHERE status = 'scheduled';

CREATE TABLE IF NOT EXISTS inbox_messages (
    message_id          UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    from_address        TEXT NOT NULL,
    to_address          TEXT NOT NULL,
    cc_addresses        TEXT[],
    from_entity_id      INTEGER,
    from_user_email     TEXT,
    to_entity_id        INTEGER,
    to_user_email       TEXT,
    subject             TEXT NOT NULL,
    body                TEXT,
    body_format         TEXT DEFAULT 'text/markdown' NOT NULL,
    message_kind        TEXT DEFAULT 'message' NOT NULL,
    payload             JSONB DEFAULT '{}'::jsonb NOT NULL,
    thread_id           UUID DEFAULT gen_random_uuid() NOT NULL,
    in_reply_to         UUID,
    sender_cert_serial  TEXT,
    sender_signature    TEXT,
    sender_verified     BOOLEAN DEFAULT FALSE NOT NULL,
    ceigas_crossing_id  UUID,
    sent_at             TIMESTAMPTZ DEFAULT NOW() NOT NULL,
    delivered_at        TIMESTAMPTZ,
    read_at             TIMESTAMPTZ,
    archived_at         TIMESTAMPTZ,
    attachments         JSONB DEFAULT '[]'::jsonb NOT NULL,
    CONSTRAINT inbox_messages_body_format_check
        CHECK (body_format IN ('text/markdown','text/plain','text/html')),
    CONSTRAINT inbox_messages_from_required
        CHECK (from_entity_id IS NOT NULL OR from_user_email IS NOT NULL),
    CONSTRAINT inbox_messages_to_required
        CHECK (to_entity_id IS NOT NULL OR to_user_email IS NOT NULL),
    -- Kept in step with migrations/v155_group_join_and_rendezvous.sql and
    -- migrations/v165_inbox_app_delivery.sql, which re-state this constraint
    -- for an already-provisioned host. EVERY list must carry the same kinds:
    -- migrations run AFTER this file on a fresh host (entrypoint.sh:271-274),
    -- so a divergence is invisible on a new pod and fatal on an old one — the
    -- direction that is hardest to notice. That is how 'machine_request'
    -- once shipped against a vocabulary that refused it; v155 carries
    -- the measurement.
    CONSTRAINT inbox_messages_message_kind_check
        CHECK (message_kind IN (
            'message','permission_grant','permission_request',
            'endpoint_request','endpoint_grant','endpoint_revoke',
            'software_order','software_status',
            'contract_proposal','contract_accept','contract_reject',
            'system_notification',
            'decision','decision_review',
            'machine_request','group_join','rendezvous_intro',
            'app_delivery'
        ))
);
CREATE INDEX IF NOT EXISTS idx_inbox_from_entity
    ON inbox_messages (from_entity_id, sent_at DESC) WHERE from_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inbox_kind_unread
    ON inbox_messages (to_entity_id, message_kind) WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_inbox_recipient_entity
    ON inbox_messages (to_entity_id, sent_at DESC) WHERE to_entity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inbox_recipient_user
    ON inbox_messages (to_user_email, sent_at DESC) WHERE to_user_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inbox_thread
    ON inbox_messages (thread_id, sent_at);

COMMENT ON TABLE calendar_events IS
    'Ship E: per-entity calendar / scheduling rows. Was central in PME. Bridge calendar ops now dispatch into pod handlers.';
COMMENT ON TABLE inbox_messages IS
    'Ship E: per-entity inbox. Was central in PME. Bridge inbox ops now dispatch into pod handlers.';


-- ── Decision substrate ─────────────────────────────────────
-- Host emits decision proposals when it hits an ambiguity it can't reason
-- through confidently. Surfaces as an inbox message of kind='decision'.
-- User picks → POST /api/decisions/:id/resolve → answer forwards as
-- next-turn prompt and host resumes. Long-running project plans + step-away
-- execution depend on this — without it the user babysits the factory.
--
-- Auto-decision fills decided_by='auto' or 'pattern:<id>' and
-- ALSO fires an inbox message of kind='decision_review' so the human can
-- still confirm / refactor / rollback / update the codebook after the fact.
-- That review is the most-scrutinized data on the platform.
--
-- Cross-host decisions piggyback inbox's cross-entity addressing; no
-- parallel surface. cross_entity_id denormalizes for fast lookups.
CREATE TABLE IF NOT EXISTS decisions (
    id                  BIGSERIAL PRIMARY KEY,
    entity_id           INTEGER NOT NULL,
    session_id          TEXT,
    proposed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    prompt              TEXT NOT NULL,
    options             JSONB NOT NULL,         -- [{key, label, rationale, tradeoffs}, ...]
    context             JSONB DEFAULT '{}'::jsonb NOT NULL,
    -- Embedding of `prompt` for pattern clustering. Populated
    -- on propose. nearest-neighbor lookup with cosine sim > 0.80
    -- inherits pattern_id from the neighbor (self-organizing exemplar).
    prompt_embedding    vector(1024),
    decided_at          TIMESTAMPTZ,
    chosen_key          TEXT,
    chosen_rationale    TEXT,
    decided_by          TEXT,                   -- 'user'|'auto'|'pattern:<id>'
    outcome             JSONB,                  -- what happened after the choice ran
    reviewed_at         TIMESTAMPTZ,
    review_verdict      TEXT,                   -- 'confirmed'|'refactor'|'rollback'|'codebook_update'
    review_notes        TEXT,
    -- pattern_id is a self-pointer to the cluster exemplar (first decided
    -- row in this semantic cluster). Set on propose when a similar
    -- decided row exists; set on resolve to `id` when this row IS the
    -- first decided one in its cluster. 4d-3 reads pattern_id to gate
    -- auto-decision confidence.
    pattern_id          INTEGER,
    supersedes_decision_id  BIGINT REFERENCES decisions(id),
    cross_entity_id     INTEGER,

    CONSTRAINT decisions_decided_by_check
        CHECK (decided_by IS NULL
            OR decided_by IN ('user','auto')
            OR decided_by LIKE 'pattern:%'),
    CONSTRAINT decisions_review_verdict_check
        CHECK (review_verdict IS NULL
            OR review_verdict IN ('confirmed','refactor','rollback','codebook_update')),
    CONSTRAINT decisions_resolved_consistency
        CHECK ((decided_at IS NULL) = (chosen_key IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_decisions_entity_pending
    ON decisions (entity_id, proposed_at DESC) WHERE decided_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_entity_decided
    ON decisions (entity_id, decided_at DESC) WHERE decided_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_session
    ON decisions (entity_id, session_id, proposed_at DESC) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_pending_review
    ON decisions (entity_id, decided_at DESC)
    WHERE decided_by IN ('auto') OR decided_by LIKE 'pattern:%' AND reviewed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_pattern
    ON decisions (pattern_id, proposed_at DESC) WHERE pattern_id IS NOT NULL;
-- ANN index for nearest-neighbor pattern lookup on propose.
CREATE INDEX IF NOT EXISTS idx_decisions_prompt_embedding
    ON decisions USING ivfflat (prompt_embedding vector_cosine_ops)
    WITH (lists = 50)
    WHERE prompt_embedding IS NOT NULL;
COMMENT ON TABLE decisions IS
    'Ship 4d-1: decision substrate. User > Host (option-builder) > User decides. Surfaces as inbox kind=decision (manual) or decision_review (auto, post-hoc). 4d-2 adds prompt_embedding + pattern_id self-pointer for cluster accrual.';

-- v098: error_events — the pod-local error lane. ONE recorder
-- (server/error_log.py) writes every tool/turn/stream failure here so
-- diagnosis starts at GET /api/system/errors/recent, not in log spelunking
-- (entity logs never ship to central Loki — sovereignty). Recorder enforces
-- 30-day retention on write. Keep in lockstep with migrations/v098.
CREATE TABLE IF NOT EXISTS error_events (
    id      BIGSERIAL PRIMARY KEY,
    at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    source  TEXT NOT NULL,                    -- 'tool' | 'sdk_tool' | 'turn' | 'stream'
    kind    TEXT NOT NULL DEFAULT 'error',    -- 'error' | 'denied' | 'timeout' | 'crash'
    tool    TEXT,                             -- tool name when a tool call failed
    message TEXT NOT NULL,                    -- error text (recorder truncates)
    context JSONB NOT NULL DEFAULT '{}'::jsonb -- session_id, turn_id, app_id, args preview
);
CREATE INDEX IF NOT EXISTS idx_error_events_at ON error_events (at DESC);

-- v157: tool_call_log — one row per tool call the model made, with the SERVED
-- model that made it and the gate's machine-readable reason.
-- Written by runtime/tool_call_ledger.record from the host turn and from
-- dispatched agents; read by rollup() for the routing tool's `measured` block.
-- Keep in lockstep with migrations/v157_tool_call_log.sql (the header there
-- says why the three existing ledgers could not answer this question).
CREATE TABLE IF NOT EXISTS tool_call_log (
    id            BIGSERIAL    PRIMARY KEY,
    at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    entity_id     INTEGER      NOT NULL,
    session_id    TEXT,
    lane          TEXT         NOT NULL CHECK (lane IN ('host', 'agent')),
    role          TEXT,
    provider      TEXT,
    model         TEXT,
    tool          TEXT         NOT NULL,
    reason        TEXT         NOT NULL CHECK (reason IN (
                      'ok', 'error', 'unknown_name', 'not_advertised',
                      'bad_args_json', 'args_schema', 'surface_denied',
                      'capability_denied', 'out_of_grant', 'policy_denied',
                      'blocked', 'dedup')),
    status        TEXT         NOT NULL,
    latency_ms    INTEGER,
    schema_errors JSONB        NOT NULL DEFAULT '[]'::jsonb,
    args          JSONB        NOT NULL DEFAULT '{}'::jsonb,
    -- v237: the project the call named in its args, for the project's
    -- activity feed (routes/projects.py); NULL when the call named none.
    project_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tool_call_log_project_at
    ON tool_call_log (project_id, at DESC) WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tool_call_log_entity_at
    ON tool_call_log (entity_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_call_log_model_at
    ON tool_call_log (model, at DESC);

-- v158: model_route_log — append-only history of every route the routing
-- table has held, with set_by = user | decision | host | host_revert.
-- Written by inference.apply_role_route; read by the host's
-- model_routing tool for the "revert is free" rule. Keep in lockstep with
-- migrations/v158_model_route_log.sql (the header there says why).
CREATE TABLE IF NOT EXISTS model_route_log (
    id                BIGSERIAL    PRIMARY KEY,
    at                TIMESTAMPTZ  NOT NULL DEFAULT now(),
    entity_id         INTEGER      NOT NULL,
    role              TEXT         NOT NULL,
    provider          TEXT         NOT NULL,
    model             TEXT         NOT NULL,
    effort            TEXT,
    fallback_provider TEXT,
    fallback_model    TEXT,
    set_by            TEXT         NOT NULL
                          CHECK (set_by IN ('user', 'decision', 'host', 'host_revert', 'canary')),
    decision_id       BIGINT,
    rejected_provider TEXT,
    rejected_model    TEXT
);
CREATE INDEX IF NOT EXISTS idx_model_route_log_role_at
    ON model_route_log (entity_id, role, at DESC);

-- v159: model_bench_runs — every run of the text-role admission bench
-- (runtime/model_bench.py) with its verdict and evidence.
-- Keep in lockstep with migrations/v159_model_bench_runs.sql.
CREATE TABLE IF NOT EXISTS model_bench_runs (
    id            BIGSERIAL    PRIMARY KEY,
    at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    entity_id     INTEGER      NOT NULL,
    provider      TEXT         NOT NULL,
    model         TEXT         NOT NULL,
    served_model  TEXT,
    suite_version TEXT         NOT NULL,
    suite_hash    TEXT         NOT NULL,
    verdict       TEXT         NOT NULL CHECK (verdict IN ('pass', 'fail', 'error')),
    reasons       JSONB        NOT NULL DEFAULT '[]'::jsonb,
    totals        JSONB        NOT NULL DEFAULT '{}'::jsonb,
    scenarios     JSONB        NOT NULL DEFAULT '[]'::jsonb,
    est_cost_usd  DOUBLE PRECISION,
    elapsed_ms    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_model_bench_runs_model_at
    ON model_bench_runs (entity_id, lower(provider), lower(model), at DESC);

-- v122: support_tickets — the Tickets pane's platform-support lane.
-- Keep in lockstep with migrations/v122_support_tickets.sql (the header there says why).
CREATE TABLE IF NOT EXISTS support_tickets (
    id              SERIAL      PRIMARY KEY,
    ticket_uuid     UUID        NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    entity_id       INTEGER     NOT NULL,

    category        TEXT        NOT NULL
                    CHECK (category IN ('bug', 'question', 'feature', 'billing', 'access')),
    -- Identified by project: a Workbench project, or NULL = the Host itself.
    project_id      INTEGER,
    project_name    TEXT        NOT NULL DEFAULT '',
    title           TEXT        NOT NULL,
    severity        TEXT        NOT NULL DEFAULT 'normal'
                    CHECK (severity IN ('low', 'normal', 'high')),

    -- The host's own card, in the same four-part shape the operator reads:
    -- problem / context / recommendation / pushback. Plain English.
    problem         TEXT        NOT NULL DEFAULT '',
    context         TEXT        NOT NULL DEFAULT '',
    recommendation  TEXT        NOT NULL DEFAULT '',
    pushback        TEXT        NOT NULL DEFAULT '',
    repro_steps     TEXT        NOT NULL DEFAULT '',
    expected        TEXT        NOT NULL DEFAULT '',
    actual          TEXT        NOT NULL DEFAULT '',

    -- The context blob the user is asked to approve. Items are
    -- {key, label, kind, content, included} — the pane toggles `included`;
    -- only included items are sent. Redacted at assembly (secret_shapes).
    shared_context  JSONB       NOT NULL DEFAULT '[]'::jsonb,

    -- Local lifecycle (the user's side):
    --   draft      — assembled, waiting for the user to approve what is shared
    --   sent       — crossed the bridge; platform_status tracks the far side
    --   discarded  — the user dropped it before sending
    --   closed     — resolved on the platform side (fixed/rejected) or by the user
    status          TEXT        NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'sent', 'discarded', 'closed')),

    -- Platform side, mirrored on every list/get:
    --   received -> reproducing -> pr_open -> approved -> fixed
    --   needs_info (a question the user answers by reply) | rejected
    platform_id     BIGINT,
    platform_status TEXT        NOT NULL DEFAULT '',
    platform_ref    TEXT        NOT NULL DEFAULT '',   -- the operator's ticket ref, informational
    resolution      TEXT        NOT NULL DEFAULT '',
    pr_url          TEXT        NOT NULL DEFAULT '',

    -- Conversation: [{author: 'host'|'user'|'platform', body, at}]
    thread          JSONB       NOT NULL DEFAULT '[]'::jsonb,

    opened_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at         TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    metadata        JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_support_tickets_entity_status
    ON support_tickets (entity_id, status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_support_tickets_platform
    ON support_tickets (platform_id) WHERE platform_id IS NOT NULL;

COMMENT ON TABLE support_tickets IS
    'Platform-support tickets the host raises for the user. Nothing leaves the pod until the user approves shared_context; the approved copy is sent over the bridge (platform.ticket).';

-- v167: circles_threads — a Circles thread is a scope, the way a Workbench
-- project already is one. Keep in lockstep with migrations/v167_circles_threads.sql.
CREATE TABLE IF NOT EXISTS circles_threads (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The FK every entity-owned table above carries (entity_episodes:109,
    -- entity_memory:136, entity_sessions:314).
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
-- user's behalf, or a Split, each write one row. A link is BETWEEN two
-- threads, so from_thread = to_thread is refused: a self-link is what a
-- non-canonical ref compared as a string produces, and it renders a thread
-- as its own RELATED item.
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

-- v222: pending_words — words the person typed into a running turn, held from
-- the moment the server receives them until a turn has them (the row goes
-- then). Keep in lockstep with migrations/v222_pending_words.sql, which says
-- why.
CREATE TABLE IF NOT EXISTS pending_words (
    id           UUID         PRIMARY KEY,
    entity_id    INTEGER      NOT NULL,
    session_id   TEXT         NOT NULL,
    thread_key   TEXT,
    room         JSONB,
    text         TEXT         NOT NULL,
    tags         JSONB,
    client_id    TEXT,
    -- The app that sent each id (pending_words.sender_of): one, or a JSON
    -- list beside a list of ids. A resend matches the same app's id only.
    client_app   TEXT,
    received_at  TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_pending_words_thread
    ON pending_words (entity_id, thread_key, received_at);

-- v219: circles_page_marks — the seen mark of each Circles conversation, keyed
-- by kind and reference (the original conversation, a peer Host, a person and
-- a room have no row of their own to hold one), and on the Host's own pages
-- the turns the person watched end. Keep in lockstep with
-- migrations/v219_circles_page_marks.sql.
CREATE TABLE IF NOT EXISTS circles_page_marks (
    entity_id   INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    ref         TEXT NOT NULL,
    reader      TEXT NOT NULL DEFAULT '',
    seen_at     TIMESTAMPTZ NOT NULL,
    seen_turns  TEXT[],
    CONSTRAINT circles_page_marks_pkey PRIMARY KEY (entity_id, kind, ref, reader),
    CONSTRAINT circles_page_marks_kind_check
        CHECK (kind IN ('self', 'entity', 'dm', 'circle')),
    CONSTRAINT circles_page_marks_ref_check
        CHECK (length(ref) BETWEEN 1 AND 320),
    CONSTRAINT circles_page_marks_reader_check
        CHECK ((kind = 'dm') = (reader <> '')),
    CONSTRAINT circles_page_marks_seen_turns_check
        CHECK (seen_turns IS NULL
               OR (kind = 'self'
                   AND cardinality(seen_turns) <= 32
                   AND array_position(seen_turns, NULL) IS NULL))
);

-- v223: turn_tool_results — a tool result too big to ride the turn's message
-- list whole, kept here once and paged by handle (runtime/result_store.py).
-- Rows go when their session is no longer open (session_handler.cleanup).
-- Keep in lockstep with migrations/v223_turn_tool_results.sql.
CREATE TABLE IF NOT EXISTS turn_tool_results (
    id            BIGSERIAL    PRIMARY KEY,
    entity_id     INTEGER      NOT NULL,
    session_id    TEXT,
    turn_token    TEXT         NOT NULL,
    tool_call_id  TEXT         NOT NULL,
    tool          TEXT         NOT NULL,
    chars         INTEGER      NOT NULL,
    content       TEXT         NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT turn_tool_results_call_key UNIQUE (turn_token, tool_call_id)
);

CREATE INDEX IF NOT EXISTS idx_turn_tool_results_session
    ON turn_tool_results (session_id);

-- (v224, code_repos and code_chunks, is not mirrored here: code_chunks
-- itself comes from migrations/v200, which this file does not mirror either.)

-- (v226, task_work_items and task_runs.kind/origin, is not mirrored here:
-- task_runs (v209) and projects come from migrations this file does not
-- mirror either. migrations/v226_task_work_items.sql says what it holds.)

-- v225: the Host's ROOT thread (runtime/root_turn.py): circles_threads
-- .builtin_type = 'root' marks it; one live root thread per Host. Keep in
-- lockstep with migrations/v225_circles_root_thread.sql, which says why. The
-- live-root index reads deleted_at (v189), declared here too so this file
-- still loads whole in its one transaction on an empty database.
ALTER TABLE circles_threads ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE circles_threads
    ADD COLUMN IF NOT EXISTS builtin_type TEXT NULL;
ALTER TABLE circles_threads DROP CONSTRAINT IF EXISTS circles_threads_builtin_type_check;
ALTER TABLE circles_threads ADD CONSTRAINT circles_threads_builtin_type_check
    CHECK (builtin_type IS NULL OR builtin_type IN ('root'));
CREATE UNIQUE INDEX IF NOT EXISTS uq_circles_threads_live_builtin
    ON circles_threads (entity_id, builtin_type)
    WHERE builtin_type IS NOT NULL AND deleted_at IS NULL;

-- ============================================================
-- generated_images (v169) — the bytes of an image this Host
-- generated, when the provider returned them inline (b64_json) rather than as a
-- link. Served back at /api/images/{id}; `id` is 128 random bits because that
-- route takes no API token. Keep in lockstep with
-- migrations/v169_generated_images.sql.
-- ============================================================
CREATE TABLE IF NOT EXISTS generated_images (
    id            TEXT PRIMARY KEY,
    entity_id     INTEGER NOT NULL
                  REFERENCES entity_registry(id) ON DELETE CASCADE,
    content_type  TEXT NOT NULL,
    bytes         BYTEA NOT NULL,
    prompt        TEXT,
    model         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_generated_images_entity_created
    ON generated_images (entity_id, created_at DESC);

-- peer_cards (v170) — what another Host shared about itself (its
-- owner's name, its phone number) when a connection was accepted. The number is
-- kept only once the platform confirmed it belongs to that Host. Mirrors
-- migrations/v170_peer_cards.sql.
CREATE TABLE IF NOT EXISTS peer_cards (
    peer_entity_id     INTEGER      PRIMARY KEY,
    owner_name         TEXT,
    phone              TEXT,
    phone_verified_at  TIMESTAMPTZ,
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT now()
);
