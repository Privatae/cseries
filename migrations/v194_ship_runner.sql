-- v194: the ship runner's records — pipelines, runs, steps.
--
-- Ships: a Host ships through a deterministic runner, no model in the path. A
-- run is one ship of one pipeline at one commit. The person's device signs ONE
-- plan for the run (bastion 0.46.0, ops_plan.go), each machine runs its own
-- lines of it and returns a receipt signed with its own key, and the runner
-- moves the run on from those receipts. These rows are the runner's state and
-- the Ships app's source; status comes from them and the receipts, never from
-- a model.
--
-- One row per fact, with first_seen and last_seen, the Oversight ledger's rule
-- (v156): a step's row moves, it is never a pile.

CREATE TABLE IF NOT EXISTS ship_pipelines (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    -- The parsed privatae.ship/v1 pipeline (server/ships/pipeline.py), and the
    -- sha256 of its canonical form: a changed pipeline is a new sha.
    spec         JSONB NOT NULL,
    spec_sha256  TEXT NOT NULL,
    -- Where it was read from: the project, the module and the file.
    source       TEXT NOT NULL DEFAULT '',
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ship_runs (
    id              BIGSERIAL PRIMARY KEY,
    pipeline_id     BIGINT NOT NULL REFERENCES ship_pipelines(id),
    -- The pipeline as it was when the run started: a pipeline changed during
    -- a run does not change the run.
    spec            JSONB NOT NULL,
    spec_sha256     TEXT NOT NULL,
    commit_sha      TEXT NOT NULL,
    trigger         TEXT NOT NULL CHECK (trigger IN ('merge', 'schedule', 'manual')),
    started_by      TEXT NOT NULL DEFAULT '',
    -- planned → awaiting_tap → running → passed | failed; undoing → undone;
    -- or stopped (refused, denied, expired, a machine that would not take a
    -- line, or stopped by the person).
    state           TEXT NOT NULL CHECK (state IN ('planned', 'awaiting_tap', 'running', 'passed',
                                                   'failed', 'undoing', 'undone', 'stopped')),
    -- The line the run is on (1-based), 0 before the first.
    step            INTEGER NOT NULL DEFAULT 0,
    -- The draft the device was asked to sign; the device and its ask
    -- ({token_id, hostname, id, keys}); the signed plan once the person
    -- tapped, its nonce, its sha256 (what receipts are keyed to) and expiry.
    plan_draft      JSONB,
    device_ask      JSONB,
    plan_envelope   TEXT,
    plan_nonce      TEXT,
    plan_sha256     TEXT,
    plan_expires    TIMESTAMPTZ,
    -- The step whose failed receipt the undo runs on.
    undo_from       INTEGER,
    -- The person pressed Stop: no further line is sent; a line already on a
    -- machine is read back first.
    stop_requested  BOOLEAN NOT NULL DEFAULT false,
    -- A failure a model is to look at (the step's rule was `ask`), and
    -- whether the Host was woken for it.
    needs_host      BOOLEAN NOT NULL DEFAULT false,
    host_woken      BOOLEAN NOT NULL DEFAULT false,
    -- When the person turned it down before approving (a no on the device, or
    -- Stop): the Host may not ask again for that pipeline for 24h
    -- (runner.HOST_REASK_AFTER_REFUSAL). The device's own 24h lock keys on the
    -- whole plan, whose expiry differs every time.
    refused_at      TIMESTAMPTZ,
    error           TEXT,
    finished_at     TIMESTAMPTZ,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One run of a pipeline at a time: the runbook's ship lease, in the schema.
CREATE UNIQUE INDEX IF NOT EXISTS ship_runs_one_active_idx
    ON ship_runs (pipeline_id) WHERE state IN ('planned', 'awaiting_tap', 'running', 'undoing');

CREATE TABLE IF NOT EXISTS ship_steps (
    run_id        BIGINT NOT NULL REFERENCES ship_runs(id),
    n             INTEGER NOT NULL,
    step          TEXT NOT NULL,
    machine       TEXT NOT NULL,
    -- waiting → sent → passed | failed; undoing → undone once its undo ran.
    state         TEXT NOT NULL CHECK (state IN ('waiting', 'sent', 'passed', 'failed', 'undoing', 'undone')),
    -- Attempts sent to the machine; the machine's own record decides which
    -- attempt may run (ops_plan_linux.go planBeginAttempt).
    attempts      INTEGER NOT NULL DEFAULT 0,
    sent_at       TIMESTAMPTZ,
    -- How long a sent line (or its undo) may go without a receipt before the
    -- runner stops and says so: from the op's time limit and its verbs.
    stale_s       INTEGER,
    -- The machine's signed line receipt (and its undo's), as it came back.
    receipt       JSONB,
    undo_receipt  JSONB,
    error         TEXT,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, n)
);

-- The Host pins each machine's encryption key the first time it uses it: a
-- ship step's credential is sealed to it (bastion ops_seal.go), and a key
-- that changes afterwards is a different machine answering, so nothing is
-- sealed to it until the person clears the pin.
CREATE TABLE IF NOT EXISTS ship_machine_keys (
    token_id        INTEGER PRIMARY KEY,
    hostname        TEXT NOT NULL,
    encryption_key  TEXT NOT NULL,
    machine_key     TEXT NOT NULL,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF to_regclass('public.ship_runs') IS NOT NULL THEN
        COMMENT ON TABLE ship_runs IS
            'One ship of one pipeline at one commit, moved on by the deterministic runner (server/ships/runner.py) from the receipts the machines sign.';
        COMMENT ON COLUMN ship_runs.plan_envelope IS
            'The ship plan the person''s device signed (a DSSE envelope); each machine verifies it itself.';
    END IF;
END $$;
