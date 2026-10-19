-- v141: the autofill grant table moves out of the kernel's Python
--
-- Three tables were being created by the running server, on demand, the first
-- time a feature was touched: app_config by server/applications/storage.py at
-- boot, update_notifications by server/applications/update_check.py on every
-- daily pass, and browser_form_grant by server/browser_autofill.py on the way
-- into each grant read or write. That is the last thing standing between the
-- kernel and a database role that cannot create tables at all — as long as any
-- request path can run CREATE TABLE, the role serving requests has to hold the
-- grant that lets it, and the split this aims at cannot happen. All
-- three modules keep every read and every write and lose only the DDL.
--
-- Only ONE of the three needs a declaration here.
--
-- app_config and update_notifications were already promoted out of Python once,
-- by v030 in May, which created both and then deliberately left the runtime
-- CREATE in place as a safety net. v030 is applied on every live host, so those
-- two tables are declared and present, and this file must not declare them
-- again: two migrations owning one table is two places for it to drift. v030
-- owns their shape. What this change does for them is remove the mirror in the
-- Python, which is the part v030 could not finish.
--
-- browser_form_grant has no declaration anywhere — not in init-db.sql, not in
-- any migration — which is why it sits on scripts/undeclared_tables_baseline.txt
-- and why it is present on only two of the eight hosts: on the other six
-- nobody has opened an autofill grant yet, so nothing ever ran the lazy CREATE.
-- Removing the Python without this CREATE would break the feature on those six.
--
-- WHAT THE TABLE IS FOR
--
-- The standing per-site permission that lets an unattended run use entries the
-- user already saved for that site. Revoking sets revoked_at and leaves the row
-- behind, so who allowed what and when survives a revoke; the partial unique
-- index is what keeps exactly one LIVE grant per (entity, host) while allowing
-- that history to accumulate underneath it.
--
-- LIVE VS CODE
--
-- The shape was checked against all eight hosts rather than
-- copied from the Python. On the two that have the table the columns, types,
-- defaults and all three indexes match the code exactly, so nothing here is a
-- correction and nothing needs to change a column that already exists. Where a
-- survey and a feature file disagree, live wins; here they did not disagree.
--
-- Nothing here alters an existing column, sets NOT NULL on one, or drops
-- anything. entrypoint.sh runs these with ON_ERROR_STOP=1 and a failure stops a
-- live host from booting, so this only ever adds what is missing.

BEGIN;

CREATE TABLE IF NOT EXISTS browser_form_grant (
  id          BIGSERIAL    PRIMARY KEY,
  entity_id   INT          NOT NULL,
  host        TEXT         NOT NULL,
  granted_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
  granted_by  TEXT         NOT NULL,
  revoked_at  TIMESTAMPTZ,
  revoked_by  TEXT,
  last_used_at TIMESTAMPTZ
);

-- One LIVE grant per (entity, host); revoking leaves the row for the
-- audit trail and a re-grant inserts a new one, so the history of who
-- allowed what and when survives a revoke.
CREATE UNIQUE INDEX IF NOT EXISTS idx_form_grant_live
  ON browser_form_grant (entity_id, host) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_form_grant_entity
  ON browser_form_grant (entity_id);

COMMIT;
