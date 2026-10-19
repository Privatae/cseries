-- v176: bastions update themselves when a version is published,
-- behind a canary ring (server/network/bastion_autoupdate.py).
--
-- bastion_autoupdate_settings — one row: whether this Host moves its own
-- bastions to the published version on its own. Absent row = on (bastions
-- update when a version is published).
--
-- bastion_autoupdate_watch — ring 0 only: per version, per machine, when the
-- loop TOLD it (and whether it was beating then) and when the machine was first
-- SEEN on the version by its own heartbeat, however it got there. The canary
-- verdict is judged over these rows (server/network/bastion_autoupdate.py says
-- how): a machine that took the version and went silent is the failure the
-- canary exists to catch (seen: a node updated clean, then wedged). took_at is what times the soak.
--
-- bastion_autoupdate_state — the last pass, per version, for the Network app:
-- the ring this Host is in, the stage the platform reported, and the verdict
-- ring 0 sent (if this Host is ring 0).

CREATE TABLE IF NOT EXISTS bastion_autoupdate_settings (
    id          SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bastion_autoupdate_watch (
    version         TEXT NOT NULL,
    token_id        INTEGER NOT NULL,
    told_at         TIMESTAMPTZ,
    online_at_told  BOOLEAN,
    took_at         TIMESTAMPTZ,
    PRIMARY KEY (version, token_id)
);

CREATE TABLE IF NOT EXISTS bastion_autoupdate_state (
    version      TEXT PRIMARY KEY,
    ring         SMALLINT,
    stage        TEXT,
    checked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    verdict      TEXT,
    verdict_at   TIMESTAMPTZ,
    reason       TEXT,
    -- when the user was told the canary could not finish, once per version
    -- per reason: none of their machines took it (stalled), or a machine told
    -- to take it went silent before it did (waiting)
    stalled_alerted_at  TIMESTAMPTZ,
    waiting_alerted_at  TIMESTAMPTZ
);
