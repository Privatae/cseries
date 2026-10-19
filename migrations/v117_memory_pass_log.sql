-- v117_memory_pass_log.sql
--
-- MEMORY FORMATION, MADE VISIBLE (no more silent failures; more transparency).
--
-- THE PROBLEM, observed on a live Host the day this was written.
-- Every lane was healthy — turn queue 0, graph queue 0, embedding coverage
-- 2458/2458, search_tsv complete, consolidation routed. And NONE of that was
-- observable. Three separate holes:
--
--   1. The 30-minute consolidator pass writes no record. Worse, on a host whose
--      turn queue is empty — the steady state — `_run_one_pass` returns from
--      its early branch and never reaches its own log line, so the graph pass
--      it just ran reports nothing at all. Measured: 20h uptime, ~30 passes
--      due, ZERO "turn-consolidator pass" lines in the pod log.
--   2. A failed extraction logs a warning and returns (0, 0); a failed graph
--      pass is swallowed. Neither leaves a row. This is the same shape as the
--      2026-08-04 outage, where fact derivation was dead for 16 DAYS because
--      the only evidence was an INFO line below the pod's log level.
--   3. Nothing exposes queue depth, so "is memory still forming?" had no
--      answer short of exec'ing into the pod and querying by hand.
--
-- THE FIX IS A LEDGER, NOT MORE LOGGING. One row per pass, written on EVERY
-- path including the quiet one, carrying what the pass did and — when it went
-- wrong — why. Reading `max(finished_at)` answers "is the lane alive?" without
-- trusting that anything is still running to tell you: staleness is computed by
-- the READER, so a loop that died silently still shows up as stale rather than
-- as silence. That property is the whole point; a self-reporting health check
-- cannot report that it stopped.
--
-- Deliberately per-PASS, not per-entity: an entity pod runs one loop, and the
-- pass is the unit of work that can fail. `entities_touched` carries the fan-out
-- for the multi-entity shape the worker still supports.
--
-- Bounded by construction, with NO retention sweep — deliberately, and said
-- plainly here because an earlier draft of this comment claimed a sweep that
-- did not exist, which is the precise sin this table was built to end.
-- One row per 30 minutes is ~17.5k rows/year of small fixed-width columns.
-- That is cheap enough that a reaper would be more moving parts than the
-- problem justifies, and the whole history is useful: "when did this lane last
-- work?" is exactly the question an old row answers.

CREATE TABLE IF NOT EXISTS memory_pass_log (
    id                   BIGSERIAL   PRIMARY KEY,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at          TIMESTAMPTZ,
    -- ok      — the pass completed; it may legitimately have done nothing.
    -- partial — the pass ran but at least one lane failed (see error).
    -- failed  — the pass itself raised; nothing can be trusted from it.
    status               TEXT        NOT NULL DEFAULT 'ok',
    -- Turn-extraction lane.
    turns_found          INTEGER     NOT NULL DEFAULT 0,
    turns_marked         INTEGER     NOT NULL DEFAULT 0,
    memories_derived     INTEGER     NOT NULL DEFAULT 0,
    sessions_processed   INTEGER     NOT NULL DEFAULT 0,
    entities_touched     INTEGER     NOT NULL DEFAULT 0,
    -- Entities whose `consolidation` role is unrouted. Their turns stay QUEUED
    -- and never stamped — correct behaviour, but it means memory formation is
    -- silently gated on a model route being set, which is exactly the
    -- 2026-08-04 failure. Counted so it can be surfaced rather than inferred.
    entities_unconfigured INTEGER    NOT NULL DEFAULT 0,
    -- Associative-graph lane (memory_graph.graph_extraction_pass).
    graph_queued         INTEGER     NOT NULL DEFAULT 0,
    graph_processed      INTEGER     NOT NULL DEFAULT 0,
    graph_failed         INTEGER     NOT NULL DEFAULT 0,
    -- The reason, in the provider's or the exception's own words. Never a
    -- generic shrug: "it failed" is what made the 16-day outage un-actionable.
    error                TEXT,
    duration_ms          INTEGER,
    metadata             JSONB       NOT NULL DEFAULT '{}'::jsonb
);

-- The one query the health endpoint runs: newest pass, and newest SUCCESSFUL
-- pass. Partial index keeps the success lookup cheap as the table grows.
CREATE INDEX IF NOT EXISTS idx_memory_pass_recent
    ON memory_pass_log (finished_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_memory_pass_ok
    ON memory_pass_log (finished_at DESC)
    WHERE status = 'ok';
-- Failures are the rows anyone actually goes looking for.
CREATE INDEX IF NOT EXISTS idx_memory_pass_problems
    ON memory_pass_log (started_at DESC)
    WHERE status <> 'ok';
