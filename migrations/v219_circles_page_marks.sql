-- v219: what Circles' book of pages reads for each conversation.
--
-- 1. circles_page_marks — one row per conversation the person has had open:
--    when they last had it in front of them (seen_at), and on the Host's own
--    pages the turns they watched end there (seen_turns). The Workbench keeps
--    the same two marks as columns of projects (v215, v217); a Circles
--    conversation has no row of its own to hold them (the original
--    conversation is not a circles_threads row, and a peer Host, a person or a
--    room is not a row of this database at all), so they are keyed by kind and
--    reference here instead:
--      kind 'self'   — your Host's page: ref 'original' or the thread's uuid
--      kind 'entity' — a peer Host: ref its entity id
--      kind 'dm'     — a person: ref their email, lowercased
--      kind 'circle' — a room of several Hosts: ref the circle's id
--    The kinds are GET /api/circles/thread/{kind}/{id}'s own. `reader` is the
--    operator a 'dm' conversation belongs to (the People list and its thread
--    are read per operator: routes/circles.py _list_people_threads), and ''
--    for every other kind, which the Host's operators share. Set by
--    PUT /api/circles/thread/{kind}/{id}/seen (server/circles_marks.py
--    mark_seen), which only moves seen_at forward.
--
--    seen_turns: the turn tokens of the replies the person watched arrive on
--    one of the Host's pages, newest last, at most 32 (v217 says why a token
--    and not a time). Only a 'self' page runs a turn here.
--
-- 2. Four partial indexes for the thread list's per-page reads
--    (circles_threads.list_threads), which the book polls:
--      idx_entity_memory_thread_rows, _thread_replies — a thread's newest
--        turn row and its newest reply, each one entry, so the threads' marks
--        cost follows the number of threads and never their rows. Measured
--        on scripts/test_circles_list_speed_pg.py's Host (300 threads, 100,000
--        turn rows): about 80 s for the list without them.
--      idx_entity_memory_original_rows, _original_replies — the original
--        conversation's count and newest row from the index entries alone,
--        and its newest reply as one entry. Main counted it by reading every
--        one of its rows and their metadata (108 ms of main's 216 ms list on
--        that Host; with the marks read the same way, 162 ms).
--    Their WHERE is the transcript's clause written out
--    (circles_threads._THREAD_ROWS, _THREAD_REPLIES, _ORIGINAL_ROWS,
--    _ORIGINAL_REPLIES); the planner uses them only while the two stay the
--    same text, which scripts/test_circles_list_speed_pg.py checks on the
--    plan.
--
-- One transaction and no CONCURRENTLY, for the reasons v216 gives: migrations
-- run at boot before the server takes requests, and an interrupted concurrent
-- build leaves an invalid index that IF NOT EXISTS would skip on every later
-- boot. Idempotent: CREATE ... IF NOT EXISTS. Nothing is backfilled: a
-- conversation nobody has marked has no row (seen_at reads NULL). Declared for
-- fresh Hosts in init-db.sql too.

BEGIN;

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

CREATE INDEX IF NOT EXISTS idx_entity_memory_thread_rows
    ON entity_memory (entity_id, (metadata->'source_context'->>'ref'),
                      (COALESCE(landed_at, created_at)))
    WHERE memory_type = 'turn' AND status = 'active' AND project_id IS NULL
      AND (metadata->'source_context'->>'kind') = 'thread'
      AND COALESCE(metadata->>'app_id', '') NOT IN ('circles-replay', 'circles-ab', 'scheduled_task');

CREATE INDEX IF NOT EXISTS idx_entity_memory_thread_replies
    ON entity_memory (entity_id, (metadata->'source_context'->>'ref'),
                      (COALESCE(landed_at, created_at)))
    WHERE memory_type = 'turn' AND status = 'active' AND project_id IS NULL
      AND (metadata->'source_context'->>'kind') = 'thread'
      AND COALESCE(metadata->>'app_id', '') NOT IN ('circles-replay', 'circles-ab', 'scheduled_task')
      AND (metadata->>'role') = 'assistant'
      AND COALESCE(metadata->>'source_kind', '') NOT IN ('thread_marker', 'connector_pick', 'index_pick');

CREATE INDEX IF NOT EXISTS idx_entity_memory_original_rows
    ON entity_memory (entity_id, created_at, landed_at)
    WHERE memory_type = 'turn' AND status = 'active' AND project_id IS NULL
      AND ((metadata->>'app_id') = 'circles' OR (metadata->>'app_id') IS NULL)
      AND (metadata->'source_context'->>'kind') IS DISTINCT FROM 'thread';

CREATE INDEX IF NOT EXISTS idx_entity_memory_original_replies
    ON entity_memory (entity_id, (COALESCE(landed_at, created_at)))
    WHERE memory_type = 'turn' AND status = 'active' AND project_id IS NULL
      AND ((metadata->>'app_id') = 'circles' OR (metadata->>'app_id') IS NULL)
      AND (metadata->'source_context'->>'kind') IS DISTINCT FROM 'thread'
      AND (metadata->>'role') = 'assistant'
      AND COALESCE(metadata->>'source_kind', '') NOT IN ('thread_marker', 'connector_pick', 'index_pick');

COMMIT;
