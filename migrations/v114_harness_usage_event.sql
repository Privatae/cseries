-- v114_harness_usage_event.sql
--
-- harness_usage_event — the PER-FRAME usage a coding run's own event stream
-- already carries (2026-08-24). New table; `harness_usage_event` appears
-- nowhere else in the tree (verified 2026-08-24).
--
-- THE GAP. harness_usage records one row per RUN and its tokens_total was 0
-- for every engine except claude-code. Measured on one Host 2026-08-24:
--   claude-code 278 runs / 229 with tokens      cursor 119 / 0
--   claude-code-s2  69 / 60                     grok   100 / 0
--   claude-code-s3  69 / 60                     kimi    28 / 0
--   codex           20 / 1 (scraped from stderr)  antigravity 6 / 0, opencode 3 / 0
--
-- It is NOT that the vendors withhold it. Kimi reports tokens perfectly on the
-- CHAT lane (241 calls, 241 with tokens, 2,359,068 in) — same vendor, same
-- account. The harness lane loses it: every vendor CLI emits usage in its event
-- stream, the pod runtime ALREADY normalizes it into `usage` frames
-- (system-apps/_harness_runtime/events.py — parse_claude_family, parse_grok,
-- parse_codex_events, parse_kimi_roles, parse_antigravity, parse_opencode),
-- and those frames land on the host at <session_dir>/<label>.events.jsonl.
-- Then they are dropped twice: the frontend has an explicit
-- `case "usage": return []`, and the host's _parse_usage reads run_out
-- (transcript + files), never the stream.
--
-- There is no /usage command to call instead. The real binaries were probed
-- (cseries-harness-kimi:v0.1.7, cseries-harness-codex:v0.2.9): neither has a usage
-- subcommand — /usage is an interactive TUI slash command and the pods run
-- these CLIs non-interactively. The stream is the only non-interactive source,
-- and it is already flowing.
--
-- WHY PER FRAME, not just a run total. Two reasons, and the second is the one
-- that makes this table rather than a column:
--   1. Aggregation differs by vendor. claude-family sends ONE cumulative usage
--      object on its final event; codex sends one per turn.completed; opencode
--      one per step_finish (per model call). The run total therefore needs a
--      per-harness rule (compute._USAGE_AGG), and a rule can be WRONG. Storing
--      every frame makes a wrong rule correctable from data already captured
--      instead of a number that is simply lost.
--   2. Session / 5-hour / weekly windows — tracked as quota consumption, not
--      as a usage metric. A run total cannot answer "how
--      much of this 5-hour window have I spent"; timestamped frames can.
--
-- `unmapped` holds numbers the vendor sent that no alias claimed. Only codex
-- and opencode arrive with known field names; claude, grok, kimi and
-- antigravity are passed through VERBATIM by the pod parser, so a vendor
-- renaming a field must degrade to a VISIBLE unknown rather than a silent zero
-- — which is precisely the always-zero-column defect this work exists to end.
--
-- cost_usd is VENDOR-REPORTED (opencode's step_finish carries `cost`) and is
-- the ESTIMATE lane only, beside harness_usage.vendor_cost_microdollars. It is
-- never a Privatae charge and never touches the wallet: the platform bills
-- compute, and the vendor's bill is never visible to it.
--
-- No FK to harness_usage: a frame is evidence that a run happened and must
-- outlive any curation or supersede of the run row, the same reason
-- harness_usage.phase_id (v112) carries no FK to project_phases.
CREATE TABLE IF NOT EXISTS harness_usage_event (
    id                  BIGSERIAL PRIMARY KEY,
    session_id          TEXT,
    harness             TEXT NOT NULL,
    project_id          INTEGER,
    phase_id            INTEGER,
    seq                 INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens    INTEGER NOT NULL DEFAULT 0,
    total_tokens        INTEGER NOT NULL DEFAULT 0,
    cost_usd            DOUBLE PRECISION NOT NULL DEFAULT 0,
    unmapped            JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The window queries this table exists for: "this session", "the last 5 hours",
-- "this week", per harness.
CREATE INDEX IF NOT EXISTS idx_hu_event_harness_time
    ON harness_usage_event (harness, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hu_event_session
    ON harness_usage_event (session_id);
CREATE INDEX IF NOT EXISTS idx_hu_event_phase
    ON harness_usage_event (phase_id, created_at DESC)
 WHERE phase_id IS NOT NULL;
