-- v099: entity_rate_limits retrofit.
--
-- init-db.sql grew entity_rate_limits (bootstrap for FRESH pods only) after
-- the oldest Host was provisioned, and no migration ever existed for it — so
-- every entity created before the table appeared boots without it. Confirmed
-- live (and re-verified): the other Hosts have the table,
-- the oldest does not, and brain/mastercode/rate_limiter.py reads/writes it
-- unconditionally — "RateLimiter: failed to load from PG" on every boot of
-- that Host.
--
-- Definition mirrors init-db.sql (entity_rate_limits block) verbatim — the
-- v090 lesson: a migration must produce exactly what init-db produces.

BEGIN;

CREATE TABLE IF NOT EXISTS entity_rate_limits (
    entity_id   INTEGER NOT NULL,
    call_date   DATE NOT NULL DEFAULT CURRENT_DATE,
    call_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (entity_id, call_date)
);

COMMIT;
