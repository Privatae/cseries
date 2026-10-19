-- v224 (2026-10-11): every repo the Host can read is indexed for code search.
--
-- search_my_code covers the Host's repos and its users' repos: if the Host can
-- read a repo, it is indexed. codesearch.repo_sync asks the Host's
-- gitea which repos it can read (the same /user/repos answer the gitea tool's
-- op='list' gives), clones each into the pod, indexes it into code_chunks,
-- re-indexes it when its default branch moves, and drops it when access goes.
--
-- code_repos records what that pass did per repo. It is never asked who may
-- read what: each pass asks the forge again.
--
--   repo         "owner/name" on the Host's gitea; code_chunks.project for it
--   head         the default branch's commit last indexed in full; a pass whose
--                remote head equals it does nothing for the repo
--   state        indexed | skipped (too big for the room on the disk) |
--                failed (clone or index did not finish; tried again next pass)
--   reason       the plain sentence for skipped / failed
--   state 'pending' with no head: granted in the Gitea app and not indexed
--                yet; on record so a restart before the next pass keeps it
--   need_head,   a repo refused for room AFTER its fetch: the head it was
--   need_bytes   measured at and the bytes its checkout and index need; the
--                next pass fetches again only when the head moved or that
--                much room is there
--
-- code_chunks.embedding becomes nullable: when no embedder answers, a file's
-- chunks are still written (lexical search works on them) and get their
-- vectors on a later pass (codesearch.indexer.index_project).

ALTER TABLE code_chunks ALTER COLUMN embedding DROP NOT NULL;

CREATE TABLE IF NOT EXISTS code_repos (
    repo            TEXT PRIMARY KEY,
    default_branch  TEXT,
    head            TEXT,
    forge_size_kib  BIGINT,
    disk_bytes      BIGINT,
    state           TEXT NOT NULL DEFAULT 'pending',
    reason          TEXT,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    indexed_at      TIMESTAMPTZ,
    need_head       TEXT,
    need_bytes      BIGINT
);
